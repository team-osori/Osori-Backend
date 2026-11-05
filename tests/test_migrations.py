import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


def test_migration_upgrade_and_downgrade(tmp_path):
    """Verify migration upgrade and downgrade against the observable API or database contract."""
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / 'migration.db'
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{db_path}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    assert {'users', 'auth_sessions', 'auth_rate_windows', 'used_refresh_tokens', 'projects', 'services', 'policies', 'policy_versions', 'testsets', 'testset_versions', 'test_questions', 'question_generation_requests'} <= set(inspect(engine).get_table_names())
    subprocess.run([sys.executable, '-m', 'alembic', 'downgrade', 'base'], cwd=root, env=env, check=True)
    assert 'users' not in inspect(engine).get_table_names()
    engine.dispose()


def test_session_bounds_migration_preserves_existing_history(tmp_path):
    """Backfill existing session deadlines and counters without deleting replay evidence."""
    from sqlalchemy import text
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "backfill.db"}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '20261105_03'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,login_id,name,password_hash,created_at) VALUES ('u','tester','test','hash','2026-01-01')"))
        conn.execute(text("INSERT INTO auth_sessions (id,user_id,access_hash,refresh_hash,access_expires_at,refresh_expires_at) VALUES ('s','u','a','r','2026-01-01','2026-01-08')"))
        conn.execute(text("INSERT INTO used_refresh_tokens (token_hash,session_id) VALUES ('old','s')"))
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    with engine.connect() as conn:
        count, matches = conn.execute(text('SELECT refresh_count, absolute_expires_at = refresh_expires_at FROM auth_sessions')).one()
        assert count == 1 and matches == 1
        assert conn.execute(text('SELECT COUNT(*) FROM used_refresh_tokens')).scalar_one() == 1
    engine.dispose()
