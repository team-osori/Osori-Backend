import asyncio
import io
from pathlib import Path
import tarfile

import pytest
from sqlalchemy.exc import OperationalError

from app.core.database import get_db
from app.main import app
from deploy.archive import receive, validate_members
from deploy.ssh_entrypoint import parse_command


def test_email_deployment_settings_fail_closed(monkeypatch):
    """Do not release signup without SMTP credentials and a shared signing secret."""
    from deploy.manage import validate_email_settings
    for name in ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM', 'EMAIL_VERIFICATION_SECRET', 'SMTP_PORT'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        validate_email_settings()
    for name in ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM'):
        monkeypatch.setenv(name, 'configured')
    monkeypatch.setenv('EMAIL_VERIFICATION_SECRET', 'x' * 32)
    validate_email_settings()
    monkeypatch.setenv('SMTP_PORT', '70000')
    with pytest.raises(ValueError):
        validate_email_settings()


def test_readiness_reports_release_and_database_failure(db_and_client, monkeypatch):
    _, client = db_and_client
    monkeypatch.setenv('OSORI_RELEASE', 'a' * 40)
    response = client.get('/api/ready')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'revision': 'a' * 40}

    class Offline:
        def execute(self, statement):
            raise OperationalError('SELECT 1', {}, Exception('do-not-expose-secret'))

    app.dependency_overrides[get_db] = lambda: Offline()
    response = client.get('/api/ready')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
    assert 'do-not-expose-secret' not in response.text


@pytest.mark.parametrize('command', ['', 'hostname', 'bash', 'deploy a 1 1',
    'deploy ' + 'a' * 40 + ' 1 1; id', 'deploy ' + 'a' * 40 + ' -1 1',
    'deploy ' + 'a' * 40 + ' 1 $(id)', 'deploy ' + 'a' * 40 + ' 1 1 extra'])
def test_ci_key_cannot_request_arbitrary_commands(command):
    with pytest.raises(ValueError):
        parse_command(command)


def test_ci_key_accepts_only_a_specific_release():
    assert parse_command('deploy ' + 'a' * 40 + ' 123 2') == ['a' * 40, '123', '2']


@pytest.mark.parametrize('name,kind', [('../etc/passwd', tarfile.REGTYPE),
    ('/etc/passwd', tarfile.REGTYPE), ('app/link', tarfile.SYMTYPE),
    ('app/link', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)])
def test_release_archive_rejects_escape_paths_and_links(name, kind):
    member = tarfile.TarInfo(name)
    member.type = kind
    with pytest.raises(ValueError):
        validate_members([member])


def test_release_archive_requires_complete_application(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
        member = tarfile.TarInfo('README.md')
        member.size = 4
        bundle.addfile(member, io.BytesIO(b'test'))
    stream.seek(0)
    with pytest.raises(ValueError, match='Incomplete'):
        receive(stream, tmp_path)


def test_worker_finishes_current_job_before_stopping(monkeypatch):
    from app import connection_worker

    async def scenario():
        stop = asyncio.Event()
        events = []

        async def process():
            events.append('started')
            stop.set()  # SIGTERM arrives during the current request.
            await asyncio.sleep(0)
            events.append('completed')
            return True

        monkeypatch.setattr(connection_worker, 'process_one', process)
        await connection_worker.run(stop_event=stop)
        assert events == ['started', 'completed']

    asyncio.run(scenario())


def test_release_health_requires_every_worker(tmp_path):
    """Fail release readiness when any individual API or worker unit is inactive."""
    import subprocess
    source = (Path(__file__).resolve().parents[1] / 'deploy/deploy.sh').read_text()
    start = source.index('all_services_ready() {')
    function = source[start:source.index('\n}\n', start) + 3]
    executable = tmp_path / 'systemctl'
    executable.write_text('#!/bin/sh\n[ "$3" != "$OFFLINE_UNIT" ]\n')
    executable.chmod(0o755)
    import os
    for offline in ('none', 'osori-api', 'osori-worker', 'osori-evaluation-worker'):
        script = function + '\nservices=(osori-api osori-worker osori-evaluation-worker)\nall_services_ready\n'
        result = subprocess.run(['bash', '-c', script], env={**os.environ,
            'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'], 'OFFLINE_UNIT': offline})
        assert (result.returncode == 0) == (offline == 'none')
