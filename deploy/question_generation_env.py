"""Send/install only question-generation settings; never print secrets or provider errors."""
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile

MAX_PAYLOAD = 16384
NAMES = {'QUESTION_GENERATION_PROVIDER', 'QUESTION_GENERATION_MODEL', 'QUESTION_GENERATION_API_KEY'}


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'apiKey', 'model'}:
        raise ValueError('Invalid settings')
    key, model = payload['apiKey'], payload['model']
    if not isinstance(key, str) or not re.fullmatch(r'sk-[A-Za-z0-9_-]{16,512}', key):
        raise ValueError('Invalid key')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', model):
        raise ValueError('Invalid model')
    return {'QUESTION_GENERATION_PROVIDER': 'openai', 'QUESTION_GENERATION_MODEL': model,
            'QUESTION_GENERATION_API_KEY': key}


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
            payload = {'apiKey': os.environ.get('QUESTION_GENERATION_API_KEY', ''),
                       'model': os.environ.get('QUESTION_GENERATION_MODEL', '')}
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
                subprocess.run(['systemctl', 'try-restart', 'osori-api'], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('Question generation settings installed. Values are not displayed.')
        else:
            raise ValueError('Unsupported operation')
    except Exception:
        print('Question generation configuration failed; check settings and permissions.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
