"""Run release preflight, DB backup, and migrations without emitting credentials."""
import argparse
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def preflight():
    url = make_url(os.environ['DATABASE_URL'])
    if url.drivername != 'mysql+pymysql' or url.host != '127.0.0.1' or url.database != 'osori':
        raise ValueError('Expected the local Osori MySQL database')
    Fernet(os.environ['CONNECTION_ENCRYPTION_KEY'].encode())
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    finally:
        engine.dispose()


def backup(destination):
    url = make_url(os.environ['DATABASE_URL'])
    destination = Path(destination)
    with tempfile.TemporaryDirectory(prefix='osori-mysql-') as temporary:
        config = Path(temporary) / 'client.cnf'
        # Bootstrap generates URL-safe credentials; reject option-file control characters.
        values = [url.username, url.password, url.host]
        if any(not value or any(c in value for c in '\r\n"\\') for value in values):
            raise ValueError('Invalid backup credentials')
        config.write_text('[client]\n' + '\n'.join([
            f'user="{url.username}"', f'password="{url.password}"',
            f'host="{url.host}"', f'port={url.port or 3306}']) + '\n')
        config.chmod(0o600)
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['mysqldump', f'--defaults-extra-file={config}',
                    '--single-transaction', '--no-tablespaces', '--set-gtid-purged=OFF',
                    '--databases', url.database], stdout=subprocess.PIPE, stderr=errors) as dump:
                with gzip.open(destination, 'wb') as compressed:
                    shutil.copyfileobj(dump.stdout, compressed)
                if dump.wait() != 0:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError('Database backup failed; deployment stopped')
        destination.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['preflight', 'backup', 'migrate'])
    parser.add_argument('destination', nargs='?')
    args = parser.parse_args()
    load_dotenv('/etc/osori-backend.env', override=True)
    try:
        preflight()
        if args.action == 'backup':
            backup(args.destination)
        elif args.action == 'migrate':
            subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True)
    except Exception as exc:
        print(f'Deployment {args.action} failed ({type(exc).__name__}).', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
