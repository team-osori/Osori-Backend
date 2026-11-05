"""Opt-in tests: OSORI_TEST_MYSQL_URL must point to a disposable MySQL database."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.models.auth import AuthSession
from app.services import auth


@pytest.fixture
def mysql_client():
    """Provide an isolated database-backed API client and release resources after the test."""
    url = os.environ.get('OSORI_TEST_MYSQL_URL')
    if not url:
        pytest.skip('OSORI_TEST_MYSQL_URL is not set')
    env = {**os.environ, 'DATABASE_URL': url}
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    engine = create_engine(url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        """Configure the isolated test database without touching the developer database."""
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield client, sessions, env, root
    app.dependency_overrides.clear()
    engine.dispose()


def signup_and_login(client):
    """Create a distinct test user and obtain credentials for protected API checks."""
    login_id = 'u' + uuid4().hex[:20]
    password = 'integration-password-123'
    assert client.post('/api/auth/signup', json={'loginId': login_id, 'name': 'test', 'password': password}).status_code == 201
    response = client.post('/api/auth/login', json={'loginId': login_id, 'password': password})
    assert response.status_code == 200
    return response.json()['data']


def test_mysql_concurrent_refresh_reuse(mysql_client):
    """Verify mysql concurrent refresh reuse against the observable API or database contract."""
    client, _, _, _ = mysql_client
    tokens = signup_and_login(client)
    barrier = Barrier(2)

    def refresh():
        """Apply the refresh budget before performing token rotation or replay detection."""
        barrier.wait(timeout=10)
        return client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(r.status_code for r in responses) == [200, 401]
    new = next(r.json()['data'] for r in responses if r.status_code == 200)
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + new['accessToken']}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': new['refreshToken']}).status_code == 401


def test_mysql_expiry_boundary(mysql_client, monkeypatch):
    """Verify mysql expiry boundary against the observable API or database contract."""
    client, sessions, _, _ = mysql_client
    tokens = signup_and_login(client)
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with sessions() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.access_hash == auth.digest(tokens['accessToken'])))
        session.access_expires_at = instant
        session.refresh_expires_at = instant
        db.commit()
    monkeypatch.setattr(auth, 'now', lambda: instant - timedelta(seconds=1))
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + tokens['accessToken']}).status_code == 200
    monkeypatch.setattr(auth, 'now', lambda: instant)
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + tokens['accessToken']}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).status_code == 401


def test_mysql_token_survives_fresh_server_process(mysql_client):
    """Verify mysql token survives fresh server process against the observable API or database contract."""
    client, _, env, root = mysql_client
    tokens = signup_and_login(client)
    script = """
import json, sys
from fastapi.testclient import TestClient
from app.main import app
tokens = json.load(sys.stdin)
with TestClient(app) as client:
    response = client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + tokens['accessToken']})
    assert response.status_code == 200, response.text
    refreshed = client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']})
    assert refreshed.status_code == 200, refreshed.text
    print(json.dumps(refreshed.json()['data']))
"""
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(tokens), text=True,
                            env=env, cwd=root, check=True, capture_output=True, timeout=20)
    refreshed = json.loads(result.stdout)
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + refreshed['accessToken']}).status_code == 200


def test_mysql_refresh_just_before_expiry(mysql_client, monkeypatch):
    """Verify mysql refresh just before expiry against the observable API or database contract."""
    client, sessions, _, _ = mysql_client
    tokens = signup_and_login(client)
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with sessions() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == auth.digest(tokens['refreshToken'])))
        session.refresh_expires_at = instant
        db.commit()
    monkeypatch.setattr(auth, 'now', lambda: instant - timedelta(seconds=1))
    response = client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']})
    assert response.status_code == 200


def test_mysql_project_ownership_and_concurrent_chatbot_registration(mysql_client):
    """Verify mysql project ownership and concurrent chatbot registration against the observable API or database contract."""
    client, _, _, _ = mysql_client
    owner = signup_and_login(client)
    other = signup_and_login(client)
    headers = {'Authorization': 'Bearer ' + owner['accessToken']}
    other_headers = {'Authorization': 'Bearer ' + other['accessToken']}
    created = client.post('/api/projects', json={'name': '한빛항공'}, headers=headers)
    assert created.status_code == 201
    path = created.headers['location']
    assert client.get(path, headers=other_headers).status_code == 404
    barrier = Barrier(2)

    def register():
        """Create a distinct test user and obtain credentials for protected API checks."""
        barrier.wait(timeout=10)
        return client.post(path + '/services', json={'name': '상담 챗봇'}, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert client.get(path, headers=headers).json()['data']['serviceCount'] == 1
    assert client.get(path + '/services', headers=other_headers).status_code == 404
    assert len(client.get(path + '/services', headers=headers).json()['data']['items']) == 1


def test_mysql_project_pagination(mysql_client):
    """Verify mysql project pagination against the observable API or database contract."""
    client, _, _, _ = mysql_client
    tokens = signup_and_login(client)
    headers = {'Authorization': 'Bearer ' + tokens['accessToken']}
    ids = set()
    for i in range(5):
        response = client.post('/api/projects', json={'name': f'Project {i}'}, headers=headers)
        assert response.status_code == 201
        ids.add(response.json()['data']['id'])
    found = []
    cursor = None
    while True:
        params = {'limit': 2}
        if cursor:
            params['cursor'] = cursor
        response = client.get('/api/projects', headers=headers, params=params)
        assert response.status_code == 200
        data = response.json()['data']
        found.extend(item['id'] for item in data['items'])
        cursor = data['nextCursor']
        if cursor is None:
            break
    assert len(found) == 5 and set(found) == ids


def test_mysql_expired_session_cleanup_cascades_history(mysql_client):
    """Verify mysql expired session cleanup cascades history against the observable API or database contract."""
    from app.maintenance import cleanup_expired
    from app.models.auth import UsedRefreshToken
    client, sessions, _, _ = mysql_client
    tokens = signup_and_login(client)
    response = client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']})
    assert response.status_code == 200
    new = response.json()['data']
    with sessions() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == auth.digest(new['refreshToken'])))
        session_id = row.id
        row.refresh_expires_at = auth.now() - timedelta(seconds=1)
        db.commit()
        assert cleanup_expired(db)['sessions'] >= 1
        assert db.scalar(select(UsedRefreshToken).where(UsedRefreshToken.session_id == session_id)) is None
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + new['accessToken']}).status_code == 401


def test_mysql_session_lifecycle_bounds(mysql_client, monkeypatch):
    """Enforce the final refresh count and absolute deadline in MySQL transactions."""
    from app.models.auth import UsedRefreshToken
    client, sessions, _, _ = mysql_client
    tokens = signup_and_login(client)
    with sessions() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == auth.digest(tokens['refreshToken'])))
        row.refresh_count = auth.AUTH_SESSION_MAX_REFRESHES - 1
        session_id = row.id
        db.commit()
    response = client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']})
    assert response.status_code == 200
    newest = response.json()['data']
    assert client.post('/api/auth/refresh', json={'refreshToken': newest['refreshToken']}).status_code == 401
    with sessions() as db:
        assert db.scalar(select(UsedRefreshToken).where(UsedRefreshToken.session_id == session_id)) is None
    tokens = signup_and_login(client)
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with sessions() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == auth.digest(tokens['refreshToken'])))
        row.absolute_expires_at = instant
        row.access_expires_at = instant + timedelta(days=1)
        row.refresh_expires_at = instant + timedelta(days=1)
        db.commit()
    monkeypatch.setattr(auth, 'now', lambda: instant)
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + tokens['accessToken']}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).status_code == 401
