import json
import os
import stat
import subprocess
import sys

import pytest

from deploy.question_generation_env import install, validate
from deploy.ssh_entrypoint import parse_command

PAYLOAD = {'region': 'ap-northeast-3',
           'generationModel': 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
           'judgeModel': 'global.anthropic.claude-haiku-4-5-20251001-v1:0'}


def test_atomic_settings_update_preserves_existing_secrets(tmp_path):
    target = tmp_path / 'runtime.env'
    target.write_text('DATABASE_URL=existing\nCONNECTION_ENCRYPTION_KEY=unchanged\nQUESTION_GENERATION_API_KEY=old\nEVALUATION_JUDGE_API_KEY=old\nOPENAI_API_KEY=old\nEVALUATION_JUDGE_PROVIDER=openai\n')
    assert install(PAYLOAD, target, uid=os.getuid(), gid=os.getgid()) is True
    result = target.read_text()
    assert 'DATABASE_URL=existing\nCONNECTION_ENCRYPTION_KEY=unchanged\n' in result
    assert 'API_KEY' not in result and 'openai' not in result
    assert 'QUESTION_GENERATION_PROVIDER=bedrock' in result
    assert 'EVALUATION_JUDGE_PROVIDER=bedrock' in result
    assert 'AWS_DEFAULT_REGION=ap-northeast-3' in result
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.osori-env-*'))
    assert install(PAYLOAD, target, uid=os.getuid(), gid=os.getgid()) is False
    assert parse_command('configure-question-generation') == ['--configure-question-generation']
    for command in ('configure-question-generation extra', 'configure-question-generation; id'):
        with pytest.raises(ValueError):
            parse_command(command)


@pytest.mark.parametrize('payload', [{**PAYLOAD, 'region': 'ap-northeast-3\nEVIL=1'},
    {**PAYLOAD, 'judgeModel': 'model\nEVIL=1'}, {**PAYLOAD, 'generationModel': '$(id)'},
    {**PAYLOAD, 'DATABASE_URL': 'override'}, {'apiKey': 'private-key', 'model': 'model'}, {}])
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
        env={**os.environ, 'BEDROCK_REGION': 'secret-invalid'}, capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout == '' and 'secret-invalid' not in result.stderr


def test_ci_defaults_use_bedrock_without_any_personal_key():
    from deploy.question_generation_env import DEFAULT_MODEL, DEFAULT_REGION
    from app.services.bedrock_runtime import DEFAULT_MODEL as MODEL, DEFAULT_REGION as REGION
    assert (DEFAULT_MODEL, DEFAULT_REGION) == (MODEL, REGION)
    result = subprocess.run([sys.executable, '-I', 'deploy/question_generation_env.py', 'emit'],
        env={**os.environ, 'BEDROCK_REGION': '', 'BEDROCK_GENERATION_MODEL': '', 'BEDROCK_JUDGE_MODEL': '',
             'QUESTION_GENERATION_API_KEY': 'personal-key-must-not-be-read'}, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout) == PAYLOAD
    assert 'personal-key' not in result.stdout + result.stderr
