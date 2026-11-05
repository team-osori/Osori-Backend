import json
import os
import stat
import subprocess
import sys

import pytest

from deploy.question_generation_env import install, validate
from deploy.ssh_entrypoint import parse_command

KEY = 'sk-' + 'test-key-only-' * 3
PAYLOAD = {'apiKey': KEY, 'model': 'gpt-4.1-mini-2025-04-14'}


def test_atomic_settings_update_preserves_existing_secrets(tmp_path):
    target = tmp_path / 'runtime.env'
    target.write_text('DATABASE_URL=existing\nCONNECTION_ENCRYPTION_KEY=unchanged\nQUESTION_GENERATION_API_KEY=old\n')
    assert install(PAYLOAD, target, uid=os.getuid(), gid=os.getgid()) is True
    result = target.read_text()
    assert 'DATABASE_URL=existing\nCONNECTION_ENCRYPTION_KEY=unchanged\n' in result
    assert result.count('QUESTION_GENERATION_API_KEY=') == 1
    assert 'QUESTION_GENERATION_PROVIDER=openai' in result
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.osori-env-*'))
    assert install(PAYLOAD, target, uid=os.getuid(), gid=os.getgid()) is False
    assert parse_command('configure-question-generation') == ['--configure-question-generation']
    for command in ('configure-question-generation extra', 'configure-question-generation; id'):
        with pytest.raises(ValueError):
            parse_command(command)


@pytest.mark.parametrize('payload', [{'apiKey': KEY + '\nEVIL=1', 'model': 'model'},
    {'apiKey': KEY, 'model': 'model\nEVIL=1'}, {**PAYLOAD, 'DATABASE_URL': 'override'}, {}])
def test_invalid_settings_leave_environment_untouched(tmp_path, payload):
    target = tmp_path / 'runtime.env'
    target.write_text('DATABASE_URL=existing\n')
    with pytest.raises(ValueError):
        install(payload, target, uid=os.getuid(), gid=os.getgid())
    assert target.read_text() == 'DATABASE_URL=existing\n'


def test_symlink_rejected_and_cli_errors_are_redacted(tmp_path):
    original = tmp_path / 'original'
    original.write_text('unchanged')
    link = tmp_path / 'link'
    link.symlink_to(original)
    with pytest.raises(ValueError):
        install(PAYLOAD, link, uid=os.getuid(), gid=os.getgid())
    result = subprocess.run([sys.executable, '-I', 'deploy/question_generation_env.py', 'emit'],
        env={**os.environ, 'QUESTION_GENERATION_API_KEY': 'secret-invalid', 'QUESTION_GENERATION_MODEL': 'm'}, capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == '' and 'secret-invalid' not in result.stderr
