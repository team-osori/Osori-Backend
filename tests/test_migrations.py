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
    assert {'users', 'auth_sessions', 'auth_rate_windows', 'used_refresh_tokens', 'projects', 'services',
            'chatbot_connections', 'connection_tests', 'prompts', 'prompt_versions',
            'policies', 'policy_versions', 'testsets', 'testset_versions', 'test_questions', 'question_generation_requests',
            'evaluations', 'evaluation_items', 'question_history'} <= set(inspect(engine).get_table_names())
    subprocess.run([sys.executable, '-m', 'alembic', 'check'], cwd=root, env=env, check=True)
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


def test_policy_upgrade_preserves_existing_prompt_data(tmp_path):
    """Upgrade from main's connection schema without losing teammate-owned prompt versions."""
    from datetime import datetime
    from app.models.auth import User
    from app.models.project import Project, Chatbot
    from app.models.prompt import Prompt, PromptVersion
    from sqlalchemy import select
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "main-upgrade.db"}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '20261105_05'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    current = datetime(2026, 11, 5)
    with engine.begin() as conn:
        conn.execute(User.__table__.insert().values(id='u', login_id='owner', name='Owner', password_hash='test-hash', created_at=current))
        conn.execute(Project.__table__.insert().values(id='p', owner_id='u', name='Project', revision=1, created_at=current, updated_at=current))
        conn.execute(Chatbot.__table__.insert().values(id='s', project_id='p', name='Chatbot', revision=1, created_at=current, updated_at=current))
        from sqlalchemy import Table, MetaData
        legacy_prompt = Table('prompts', MetaData(), autoload_with=conn)
        conn.execute(legacy_prompt.insert().values(id='prompt', service_id='s', name='Existing', revision=1, current_version_id='pv', created_at=current, updated_at=current))
        conn.execute(PromptVersion.__table__.insert().values(id='pv', prompt_id='prompt', version=1, content='기존 프롬프트 원문', created_at=current))
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    with engine.connect() as conn:
        assert conn.execute(select(PromptVersion.content).where(PromptVersion.id == 'pv')).scalar_one() == '기존 프롬프트 원문'
    assert 'policies' in inspect(engine).get_table_names()
    engine.dispose()


def test_email_migration_preserves_legacy_identity_and_refuses_lossy_downgrade(tmp_path):
    """Keep old account ownership and prevent dropping populated email addresses."""
    from sqlalchemy import text
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "legacy-email.db"}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '20261105_09'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,login_id,name,password_hash,created_at) VALUES ('u','legacy','test','hash','2026-01-01')"))
        conn.execute(text("INSERT INTO projects (id,owner_id,name,revision,created_at,updated_at) VALUES ('p','u','project',1,'2026-01-01','2026-01-01')"))
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT login_id,email FROM users WHERE id='u'")).one() == ('legacy', None)
        assert conn.execute(text("SELECT owner_id FROM projects WHERE id='p'")).scalar_one() == 'u'
        conn.execute(text("UPDATE users SET email='legacy@example.com' WHERE id='u'"))
    result = subprocess.run([sys.executable, '-m', 'alembic', 'downgrade', '20261105_10'], cwd=root, env=env, capture_output=True)
    assert result.returncode != 0 and b'Cannot discard stored email identities' in result.stderr
    engine.dispose()
