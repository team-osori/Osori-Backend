"""Deployment diagnostics identify configuration failures without leaking values."""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from deploy import manage


@pytest.fixture
def configured(monkeypatch):
    for name in ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM'):
        monkeypatch.setenv(name, 'private-value-do-not-log')
    monkeypatch.setenv('EMAIL_VERIFICATION_SECRET', 'private-signing-secret' * 2)
    monkeypatch.setenv('SMTP_PORT', '587')
    monkeypatch.setenv('DATABASE_URL', 'mysql+pymysql://user:private-password@127.0.0.1/osori')
    monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(manage, 'load_dotenv', lambda *a, **kw: None)
    monkeypatch.setattr(manage.sys, 'argv', ['manage.py', 'preflight'])
    monkeypatch.setattr(manage.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(manage.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='eng\nkor\n'))


def test_missing_email_settings_report_every_name(configured, monkeypatch, capsys):
    names = ('EMAIL_VERIFICATION_SECRET', 'SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM')
    for name in names:
        monkeypatch.delenv(name)
    assert manage.main() == 1
    error = capsys.readouterr().err
    assert all(name in error for name in names)
    assert 'private-' not in error


@pytest.mark.parametrize('port', ['0', '65536', 'private-invalid-port'])
def test_invalid_port_value_is_hidden(configured, monkeypatch, capsys, port):
    monkeypatch.setenv('SMTP_PORT', port)
    assert manage.main() == 1
    error = capsys.readouterr().err
    assert 'SMTP_PORT must be an integer between 1 and 65535' in error
    assert 'private-' not in error


@pytest.mark.parametrize('failure,expected', [
    ('executable', 'Install poppler-utils'),
    ('language', 'Install tesseract-ocr-kor'),
    ('database_url', 'DATABASE_URL is missing or invalid'),
    ('database_target', 'DATABASE_URL must use mysql+pymysql'),
    ('key', 'CONNECTION_ENCRYPTION_KEY is missing or invalid'),
    ('database_connection', 'Database connection check failed'),
])
def test_preflight_reports_safe_failure(configured, monkeypatch, capsys, failure, expected):
    if failure == 'executable':
        monkeypatch.setattr(manage.shutil, 'which', lambda name: None)
    elif failure == 'language':
        monkeypatch.setattr(manage.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='eng\n'))
    elif failure == 'database_url':
        monkeypatch.setenv('DATABASE_URL', 'private-invalid-url')
    elif failure == 'database_target':
        monkeypatch.setenv('DATABASE_URL', 'mysql+pymysql://user:private-password@remote/osori')
    elif failure == 'key':
        monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', 'private-invalid-key')
    else:
        def fail(*a, **kw):
            raise RuntimeError('private-password in underlying driver exception')
        monkeypatch.setattr(manage, 'create_engine', fail)
    assert manage.main() == 1
    error = capsys.readouterr().err
    assert expected in error
    assert 'private-' not in error


def test_unexpected_exception_text_stays_hidden(configured, monkeypatch, capsys):
    def fail(**kw):
        raise ValueError('private-credential')
    monkeypatch.setattr(manage, 'preflight', fail)
    assert manage.main() == 1
    assert capsys.readouterr().err == 'Deployment preflight failed (ValueError).\n'


def test_successful_preflight_disposes_connection(configured, monkeypatch):
    from unittest.mock import MagicMock
    engine = MagicMock()
    monkeypatch.setattr(manage, 'create_engine', lambda url: engine)
    assert manage.main() == 0
    engine.connect.return_value.__enter__.return_value.execute.assert_called_once()
    engine.dispose.assert_called_once()
