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
