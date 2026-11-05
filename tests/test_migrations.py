import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


def test_migration_upgrade_and_downgrade(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / 'migration.db'
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{db_path}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    assert {'users', 'auth_sessions', 'auth_rate_windows', 'used_refresh_tokens', 'projects', 'services'} <= set(inspect(engine).get_table_names())
    subprocess.run([sys.executable, '-m', 'alembic', 'downgrade', 'base'], cwd=root, env=env, check=True)
    assert 'users' not in inspect(engine).get_table_names()
    engine.dispose()
