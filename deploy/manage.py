"""Run release preflight, DB backup, and migrations without emitting credentials."""
import argparse
import os
import shutil
from pathlib import Path
import subprocess
import sys

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def preflight():
    # Fail before stopping the current release if OCR dependencies are missing.
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        raise ValueError("Install poppler-utils and tesseract-ocr with kor/eng language data")
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=True, timeout=5)
    if not {"kor", "eng"} <= set(languages.stdout.split()):
        raise ValueError("Install tesseract-ocr-kor and tesseract-ocr-eng")
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
    """Compatibility for old deployment tools; new tools call isolated backup.py directly."""
    subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).with_name('backup.py')), str(destination)], check=True)


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
            subprocess.run([sys.executable, '-I', '-m', 'alembic', 'upgrade', 'head'], check=True)
    except Exception as exc:
        print(f'Deployment {args.action} failed ({type(exc).__name__}).', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
