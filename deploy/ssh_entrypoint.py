#!/usr/bin/python3
"""Forced command for the CI key: accept release uploads, never an arbitrary shell."""
import os
import re
import shlex
import subprocess
import sys


def parse_command(command):
    parts = shlex.split(command)
    if parts == ['configure-question-generation']:
        return ['--configure-question-generation']
    if (len(parts) != 4 or parts[0] != 'deploy'
            or not re.fullmatch(r'[a-f0-9]{40}', parts[1])
            or not all(re.fullmatch(r'[1-9][0-9]{0,19}', value) for value in parts[2:])):
        raise ValueError('Only deploy <commit> <run> <attempt> is permitted')
    return parts[1:]


def main():
    try:
        args = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError:
        print('Deployment command rejected.', file=sys.stderr)
        return 2
    return subprocess.run(['sudo', '-n', '/usr/local/sbin/osori-deploy', *args],
                          stdin=sys.stdin, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
