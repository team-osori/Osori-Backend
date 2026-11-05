"""Run only in a disposable root container: exercise the production build and root backup."""
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    """A poisoned runtime home, Python path or venv must never execute during root backup."""
    if os.geteuid() != 0 or not Path('/.dockerenv').exists():
        raise SystemExit('This check requires a disposable root Docker container.')
    source = Path(__file__).resolve().parents[1]
    for executable in ('/usr/bin/python3', '/usr/bin/python3.12'):
        if not Path(executable).exists():
            Path(executable).symlink_to(sys.executable)
    subprocess.run(['useradd', '--create-home', '--user-group', '--home-dir',
                    '/var/lib/osori-backend', 'osori'], check=True)
    release = Path('/opt/osori-backend/releases/isolated-test')
    (release / 'deploy').mkdir(parents=True)
    (release / 'requirements.lock').write_text('')  # Ensurepip is sufficient; no network or app dependencies.
    shutil.copyfile(source / 'deploy/backup.py', release / 'deploy/backup.py')
    (release / 'deploy/backup.py').chmod(0o664)  # Reject writable group bits from release archives.
    expected = (release / 'deploy/backup.py').read_bytes()
    poison = "from pathlib import Path; import os; Path('/tmp/osori-startup-executed-' + str(os.geteuid())).touch()\n"
    home = Path('/var/lib/osori-backend/.local/lib/python3.12/site-packages')
    home.mkdir(parents=True)
    (home / 'sitecustomize.py').write_text(poison)
    subprocess.run(['chown', '-R', 'osori:osori', '/var/lib/osori-backend'], check=True)
    # Isolated root Python must also ignore application modules with stdlib names.
    (release / 'deploy/gzip.py').write_text(poison + "raise RuntimeError('App module imported by root')\n")
    script = (source / 'deploy/deploy.sh').read_text()
    functions = script[script.index('prepare_builder() {'):script.index('# Bootstrap and existing hosts')]
    env = {**os.environ, 'HOME': '/var/lib/osori-backend', 'PYTHONUSERBASE': '/var/lib/osori-backend/.local',
           'PYTHONPATH': str(home), 'PYTHONHOME': '/nonexistent-python-home'}
    subprocess.run(['/bin/bash', '-ec', functions + '\nprepare_builder\nbuild_release "$1"\n',
                    'isolation-check', str(release)], env=env, check=True)
    assert not list(Path('/tmp').glob('osori-startup-executed-*')), 'Runtime startup code reached the builder'
    assert (release / 'deploy/backup.py').read_bytes() == expected
    for account in ('osori', 'osori-build'):
        attempt = subprocess.run(['runuser', '-u', account, '--', '/usr/bin/python3', '-I', '-c',
            'from pathlib import Path; Path("/opt/osori-backend/releases/isolated-test/deploy/backup.py").write_text("changed")'],
            capture_output=True)
        assert attempt.returncode != 0, f'{account} modified the privileged backup script'
    attempt = subprocess.run(['runuser', '-u', 'osori', '--', '/usr/bin/python3', '-I', '-c',
        'from pathlib import Path; Path("/var/lib/osori-build/injected").write_text("changed")'], capture_output=True)
    assert attempt.returncode != 0, 'Runtime UID modified the build home'
    # Even a compromised dependency inside the frozen app venv is outside root's import path.
    for path in (release / '.venv/lib').glob('python*/site-packages'):
        (path / 'sitecustomize.py').write_text(poison)
    environment = Path('/etc/osori-backend.env')
    environment.write_text('DATABASE_URL="mysql+pymysql://osori_app:dummy-only@127.0.0.1:3306/osori?charset=utf8mb4"\n')
    environment.chmod(0o640)
    fake_dump = Path('/usr/bin/mysqldump')
    fake_dump.write_text('#!/bin/sh\nprintf "SELECT 1;\\n"\n')
    fake_dump.chmod(0o755)
    destination = Path('/var/backups/osori-backend/test.sql.gz')
    destination.parent.mkdir(parents=True, mode=0o700)
    subprocess.run(['/usr/bin/python3', '-I', str(release / 'deploy/backup.py'), str(destination)], env=env, check=True)
    assert gzip.decompress(destination.read_bytes()) == b'SELECT 1;\n'
    assert not list(Path('/tmp').glob('osori-startup-executed-*')), 'Root executed runtime or venv startup code'
    assert (release / 'deploy/backup.py').read_bytes() == expected
    print('Deployment isolation passed: separate builder, immutable source, isolated root backup.')


if __name__ == '__main__':
    main()
