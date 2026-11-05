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
            'evaluations', 'evaluation_items', 'question_history', 'prompt_suggestions'} <= set(inspect(engine).get_table_names())
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


def test_identity_cleanup_preserves_users_sessions_and_memberships(tmp_path):
    """Upgrade an already migrated database without changing account credentials or access."""
    from sqlalchemy import text
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "identity-cleanup.db"}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '20261105_32'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,login_id,email,name,password_hash,created_at) VALUES ('u','legacy','legacy@example.com','test','hash','2026-01-01'), ('m','member',NULL,'member','member-hash','2026-01-01')"))
        conn.execute(text("INSERT INTO projects (id,owner_id,name,revision,created_at,updated_at) VALUES ('p','u','project',1,'2026-01-01','2026-01-01')"))
        conn.execute(text("INSERT INTO project_members (id,project_id,user_id,invited_by,role,status,revision,created_at) VALUES ('pm','p','m','u','member','active',1,'2026-01-01')"))
        conn.execute(text("INSERT INTO auth_sessions (id,user_id,access_hash,refresh_hash,absolute_expires_at,refresh_count,access_expires_at,refresh_expires_at) VALUES ('s','u','a','r','2026-12-01',0,'2026-12-01','2026-12-01')"))
        conn.execute(text("INSERT INTO email_verifications (id,email,code_hash,expires_at) VALUES ('v','legacy@example.com','hash','2026-12-01')"))
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    assert 'email_verifications' not in inspect(engine).get_table_names()
    assert 'email' not in {c['name'] for c in inspect(engine).get_columns('users')}
    with engine.connect() as conn:
        assert conn.execute(text('SELECT id,login_id,password_hash FROM users ORDER BY id')).all() == [('m', 'member', 'member-hash'), ('u', 'legacy', 'hash')]
        assert conn.execute(text("SELECT owner_id FROM projects WHERE id='p'")).scalar_one() == 'u'
        assert conn.execute(text("SELECT user_id,role,status FROM project_members WHERE id='pm'")).one() == ('m', 'member', 'active')
        assert conn.execute(text("SELECT user_id,access_hash,refresh_hash FROM auth_sessions WHERE id='s'")).one() == ('u', 'a', 'r')
    subprocess.run([sys.executable, '-m', 'alembic', 'check'], cwd=root, env=env, check=True)
    subprocess.run([sys.executable, '-m', 'alembic', 'downgrade', '20261105_32'], cwd=root, env=env, check=True)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM users')).scalar_one() == 2
        assert conn.execute(text('SELECT COUNT(email) FROM users')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM email_verifications')).scalar_one() == 0
    engine.dispose()
