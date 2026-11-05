import gzip
import io
from pathlib import Path
import stat

import pytest

from deploy import backup


def environment(tmp_path, value='mysql+pymysql://app:test%2Bpassword@127.0.0.1:3306/osori?charset=utf8mb4'):
    """Write isolated dummy credentials without reading a developer or server environment file."""
    path = tmp_path / 'server.env'
    path.write_text('DATABASE_URL="' + value + '"\nCORS_ORIGINS="http://one,http://two"\n')
    return path


def test_backup_parses_quoted_and_encoded_credentials(tmp_path):
    """Read the existing server format and decode URL credentials without dotenv or SQLAlchemy."""
    assert backup.database_settings(environment(tmp_path)) == ('app', 'test+password', 3306)


@pytest.mark.parametrize('value', [
    'mysql+pymysql://app:test@remote.example/osori',
    'mysql+pymysql://app:test@127.0.0.1/other',
    'mysql+pymysql://app:test%0Ainjected@127.0.0.1/osori',
    'mysql+pymysql://app:test%00injected@127.0.0.1/osori',
    'mysql+pymysql://app:test%22injected@127.0.0.1/osori',
    'mysql+pymysql://app:test@127.0.0.1:99999/osori',
])
def test_backup_rejects_remote_targets_and_option_file_injection(tmp_path, value):
    """Root backup supports only the configured local application DB and safe client options."""
    with pytest.raises(ValueError):
        backup.database_settings(environment(tmp_path, value))


@pytest.mark.parametrize('exit_code', [0, 1])
def test_backup_streaming_permissions_and_partial_cleanup(tmp_path, monkeypatch, exit_code):
    """Keep valid gzip backups private and remove incomplete output when mysqldump fails."""
    class Dump:
        def __init__(self, command, **kwargs):
            assert command[0] == '/usr/bin/mysqldump'
            config = Path(command[1].split('=', 1)[1])
            assert stat.S_IMODE(config.stat().st_mode) == 0o600
            assert 'password="test+password"' in config.read_text()
            self.stdout = io.BytesIO(b'-- test dump\nSELECT 1;\n')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stdout.close()
        def wait(self):
            return exit_code
    monkeypatch.setattr(backup.subprocess, 'Popen', Dump)
    destination = tmp_path / 'dump.sql.gz'
    if exit_code:
        with pytest.raises(RuntimeError):
            backup.backup(destination, environment(tmp_path))
        assert not destination.exists()
    else:
        backup.backup(destination, environment(tmp_path))
        assert gzip.decompress(destination.read_bytes()) == b'-- test dump\nSELECT 1;\n'
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_does_not_remove_existing_history(tmp_path):
    """Exclusive output creation must preserve previous backups if a destination is reused."""
    destination = tmp_path / 'existing.sql.gz'
    destination.write_bytes(b'preserved')
    with pytest.raises(FileExistsError):
        backup.backup(destination, environment(tmp_path))
    assert destination.read_bytes() == b'preserved'
