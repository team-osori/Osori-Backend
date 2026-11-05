"""Install Bedrock generation/judgment settings without API keys or AWS credentials."""
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile

MAX_PAYLOAD = 16384
DEFAULT_MODEL = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'
DEFAULT_REGION = 'ap-northeast-3'
NAMES = {'QUESTION_GENERATION_PROVIDER', 'QUESTION_GENERATION_MODEL', 'QUESTION_GENERATION_API_KEY',
         'EVALUATION_JUDGE_PROVIDER', 'EVALUATION_JUDGE_MODEL', 'EVALUATION_JUDGE_API_KEY',
         'OPENAI_API_KEY', 'AWS_REGION', 'AWS_DEFAULT_REGION'}


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'region', 'generationModel', 'judgeModel'}:
        raise ValueError('Invalid settings')
    region, generation, judge = payload['region'], payload['generationModel'], payload['judgeModel']
    if not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+)+-\d{1,2}', region):
        raise ValueError('Invalid region')
    for model in (generation, judge):
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,2047}', model):
            raise ValueError('Invalid model')
    return {'QUESTION_GENERATION_PROVIDER': 'bedrock', 'QUESTION_GENERATION_MODEL': generation,
            'EVALUATION_JUDGE_PROVIDER': 'bedrock', 'EVALUATION_JUDGE_MODEL': judge,
            'AWS_REGION': region, 'AWS_DEFAULT_REGION': region}


def install(payload, target, *, uid, gid):
    values = validate(payload)
    target = Path(target)
    if target.is_symlink() or not target.is_file():
        raise ValueError('Expected existing environment file')
    lines = [line for line in target.read_text().splitlines()
             if line.strip().removeprefix('export ').partition('=')[0].strip() not in NAMES]
    content = '\n'.join(lines + [f'{name}={value}' for name, value in values.items()]) + '\n'
    metadata = target.stat()
    if (target.read_text() == content and metadata.st_uid == uid and metadata.st_gid == gid
            and metadata.st_mode & 0o777 == 0o640):
        return False
    fd, temporary = tempfile.mkstemp(prefix='.osori-env-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), 0o640)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main():
    try:
        if sys.argv[1:] == ['emit']:
            payload = {'region': os.environ.get('BEDROCK_REGION', '').strip() or DEFAULT_REGION,
                       'generationModel': os.environ.get('BEDROCK_GENERATION_MODEL', '').strip() or DEFAULT_MODEL,
                       'judgeModel': os.environ.get('BEDROCK_JUDGE_MODEL', '').strip() or DEFAULT_MODEL}
            validate(payload)
            # Only invoke with stdout piped directly into the restricted SSH command.
            sys.stdout.write(json.dumps(payload))
        elif sys.argv[1:] == ['install'] and os.geteuid() == 0:
            import grp
            data = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
            if len(data) > MAX_PAYLOAD:
                raise ValueError('Oversized settings')
            changed = install(json.loads(data), '/etc/osori-backend.env', uid=0, gid=grp.getgrnam('osori').gr_gid)
            if changed:
                subprocess.run(['systemctl', 'try-restart', 'osori-api', 'osori-evaluation-worker'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('Bedrock AI settings installed. Credentials are not accepted or displayed.')
        else:
            raise ValueError('Unsupported operation')
    except Exception:
        print('Bedrock configuration failed; check settings and permissions.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
