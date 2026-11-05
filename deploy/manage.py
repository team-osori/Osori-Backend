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
from sqlalchemy.exc import ArgumentError


class PreflightError(ValueError):
    """An operator-facing message containing only static text and setting names."""


def preflight():
    """Check release dependencies before stopping the running service."""
    # Fail before stopping the current release if OCR dependencies are missing.
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        raise PreflightError("Install poppler-utils, tesseract-ocr, tesseract-ocr-kor and tesseract-ocr-eng")
    try:
        languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise PreflightError('Cannot inspect Tesseract languages; check the OCR installation') from None
    if not {"kor", "eng"} <= set(languages.stdout.split()):
        raise PreflightError("Install tesseract-ocr-kor and tesseract-ocr-eng")
    try:
        url = make_url(os.environ['DATABASE_URL'])
    except (KeyError, ValueError, ArgumentError):
        raise PreflightError('DATABASE_URL is missing or invalid') from None
    if url.drivername != 'mysql+pymysql' or url.host != '127.0.0.1' or url.database != 'osori':
        raise PreflightError('DATABASE_URL must use mysql+pymysql at 127.0.0.1 with database osori')
    try:
        Fernet(os.environ['CONNECTION_ENCRYPTION_KEY'].encode())
    except (KeyError, ValueError):
        raise PreflightError('CONNECTION_ENCRYPTION_KEY is missing or invalid; preserve the existing key') from None
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except Exception:
        raise PreflightError('Database connection check failed; check local MySQL and DATABASE_URL') from None
    finally:
        if engine is not None:
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
    except PreflightError as exc:
        print(f'Deployment {args.action} failed: {exc}.', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Deployment {args.action} failed ({type(exc).__name__}).', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
