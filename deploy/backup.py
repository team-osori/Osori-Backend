"""Root-only DB backup using the system Python standard library, outside the app venv."""
import gzip
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit


def database_settings(environment_file):
    """Read a local DB URL without importing application code or evaluating shell content."""
    database_url = None
    for line in Path(environment_file).read_text().splitlines():
        parts = shlex.split(line, comments=True)
        if parts and parts[0].startswith('DATABASE_URL='):
            if len(parts) != 1:
                raise ValueError('Invalid database setting')
            database_url = parts[0].split('=', 1)[1]
    if not database_url:
        raise ValueError('Missing database setting')
    url = urlsplit(database_url)
    if (url.scheme != 'mysql+pymysql' or url.hostname != '127.0.0.1'
            or url.path != '/osori' or url.fragment):
        raise ValueError('Expected the local Osori database')
    username = unquote(url.username or '', errors='strict')
    password = unquote(url.password or '', errors='strict')
    if any(not value or any(c in value for c in '\x00\r\n"\\') for value in (username, password)):
        raise ValueError('Invalid backup credentials')
    port = url.port or 3306
    if not 1 <= port <= 65535:
        raise ValueError('Invalid database port')
    return username, password, port


def backup(destination, environment_file='/etc/osori-backend.env'):
    """Stream a backup to a new private file and remove only partial files created here."""
    username, password, port = database_settings(environment_file)
    destination = Path(destination)
    with tempfile.TemporaryDirectory(prefix='osori-mysql-') as temporary:
        config = Path(temporary) / 'client.cnf'
        config.write_text('[client]\n' + '\n'.join([
            f'user="{username}"', f'password="{password}"', 'host="127.0.0.1"', f'port={port}']) + '\n')
        config.chmod(0o600)
        # Exclusive creation prevents a failed retry from deleting an existing backup.
        with destination.open('xb') as target:
            os.chmod(destination, 0o600)
            try:
                with tempfile.TemporaryFile() as errors:
                    with subprocess.Popen(['/usr/bin/mysqldump', f'--defaults-extra-file={config}',
                            '--single-transaction', '--no-tablespaces', '--set-gtid-purged=OFF',
                            '--databases', 'osori'], stdout=subprocess.PIPE, stderr=errors) as dump:
                        with gzip.GzipFile(fileobj=target, mode='wb') as compressed:
                            shutil.copyfileobj(dump.stdout, compressed)
                        if dump.wait() != 0:
                            raise RuntimeError('Database backup failed')
            except BaseException:
                destination.unlink(missing_ok=True)
                raise


def main():
    """Emit only sanitized failure types; credentials and DB stderr never enter CI logs."""
    os.umask(0o077)
    if os.geteuid() != 0 or len(sys.argv) != 2:
        print('Run the root backup command with one destination.', file=sys.stderr)
        return 2
    try:
        backup(sys.argv[1])
    except Exception as exc:
        print(f'Database backup failed ({type(exc).__name__}).', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
