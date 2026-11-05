from datetime import timedelta

from sqlalchemy import select

from app.models.auth import AuthSession, User
from app.services.auth import digest, now


def test_auth_lifecycle(db_and_client):
    sessions, client = db_and_client
    signup = {'loginId': 'osori_dev', 'name': '담당자', 'password': 'secure-password-123'}
    assert client.get('/api/auth/login-id-availability', params={'loginId': signup['loginId']}).json()['data']['available'] is True
    created = client.post('/api/auth/signup', json=signup)
    assert created.status_code == 201
    assert created.json()['data']['loginId'] == 'osori_dev'
    assert 'password' not in created.json()['data']
    assert client.post('/api/auth/signup', json=signup).status_code == 409
    assert client.get('/api/auth/login-id-availability', params={'loginId': signup['loginId']}).json()['data']['available'] is False
    with sessions() as db:
        user = db.scalar(select(User))
        assert user.password_hash != signup['password']
    login = client.post('/api/auth/login', json={'loginId': signup['loginId'], 'password': signup['password']})
    assert login.status_code == 200
    tokens = login.json()['data']
    access, refresh = tokens['accessToken'], tokens['refreshToken']
    with sessions() as db:
        session = db.scalar(select(AuthSession))
        assert session.access_hash == digest(access) and session.refresh_hash == digest(refresh)
        assert access != session.access_hash and refresh != session.refresh_hash
    assert client.get('/api/auth/me', headers={'Authorization': f'Bearer {access}'}).json()['data']['id'] == created.json()['data']['id']
    rotated = client.post('/api/auth/refresh', json={'refreshToken': refresh})
    assert rotated.status_code == 200
    assert client.get('/api/auth/me', headers={'Authorization': f'Bearer {access}'}).status_code == 401
    new = rotated.json()['data']
    assert client.post('/api/auth/logout', json={'refreshToken': new['refreshToken']}).status_code == 204
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {new['accessToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': new['refreshToken']}).status_code == 401


def test_errors_and_expiration(db_and_client):
    sessions, client = db_and_client
    assert client.post('/api/auth/signup', json={'loginId': 'x', 'name': 'a', 'password': 'short'}).status_code == 400
    signup = {'loginId': 'tester', 'name': 'Test', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json=signup)
    wrong = client.post('/api/auth/login', json={'loginId': 'tester', 'password': 'wrong-password-123'})
    missing = client.post('/api/auth/login', json={'loginId': 'missing', 'password': 'wrong-password-123'})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()['error']['code'] == missing.json()['error']['code'] == 'INVALID_CREDENTIALS'
    assert wrong.json()['error']['message'] == missing.json()['error']['message']
    assert client.get('/api/auth/me').status_code == 401
    tokens = client.post('/api/auth/login', json={'loginId': 'tester', 'password': signup['password']}).json()['data']
    with sessions() as db:
        session = db.scalar(select(AuthSession))
        session.access_expires_at = now() - timedelta(seconds=1)
        session.refresh_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {tokens['accessToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).status_code == 401
    assert client.post('/api/auth/logout', json={'refreshToken': tokens['refreshToken']}).status_code == 401


def test_sessions_are_independent_and_invalid_tokens_are_rejected(db_and_client):
    _, client = db_and_client
    signup = {'loginId': 'tester', 'name': 'Test', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=signup).status_code == 201
    credentials = {'loginId': signup['loginId'], 'password': signup['password']}
    first = client.post('/api/auth/login', json=credentials).json()['data']
    second = client.post('/api/auth/login', json=credentials).json()['data']
    assert client.post('/api/auth/logout', json={'refreshToken': first['refreshToken']}).status_code == 204
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {first['accessToken']}"}).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {second['accessToken']}"}).status_code == 200
    assert client.post('/api/auth/refresh', json={'refreshToken': 'forged'}).status_code == 401
    assert client.post('/api/auth/logout', json={'refreshToken': ''}).status_code == 400
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {second['refreshToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': second['accessToken']}).status_code == 401


def test_signup_common_rules_and_body_limits(db_and_client):
    _, client = db_and_client
    body = {'loginId': 'tester', 'name': '  담당자  ', 'password': ' secure-password-123 '}
    response = client.post('/api/auth/signup', json=body)
    assert response.status_code == 201
    assert response.headers['location'] == '/api/auth/me'
    assert response.json()['data']['name'] == '담당자'
    assert client.post('/api/auth/login', json={'loginId': 'tester', 'password': body['password']}).status_code == 200
    assert client.post('/api/auth/login', json={'loginId': 'tester', 'password': body['password'].strip()}).status_code == 401
    assert client.post('/api/auth/signup', json={**body, 'extra': True}).status_code == 400
    assert client.post('/api/auth/login', content='{}', headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post('/api/auth/login', content=b' ' * (1024 * 1024 + 1), headers={'Content-Type': 'application/json'}).status_code == 413


def test_login_id_is_normalized(db_and_client):
    _, client = db_and_client
    signup = {'loginId': 'Osori_DEV', 'name': 'Test', 'password': 'secure-password-123'}
    response = client.post('/api/auth/signup', json=signup)
    assert response.status_code == 201
    assert response.json()['data']['loginId'] == 'osori_dev'
    assert client.get('/api/auth/login-id-availability', params={'loginId': 'OSORI_dev'}).json()['data'] == {'loginId': 'osori_dev', 'available': False}
    assert client.post('/api/auth/signup', json={**signup, 'loginId': 'osori_dev'}).status_code == 409
    assert client.post('/api/auth/login', json={'loginId': 'OSORI_DEV', 'password': signup['password']}).status_code == 200


def test_error_details_and_request_id(db_and_client):
    _, client = db_and_client
    response = client.post('/api/auth/signup', json={'loginId': 'tester', 'password': 'secure-password-123'})
    assert response.status_code == 400
    error = response.json()['error']
    assert error['requestId'] == response.headers['x-request-id']
    assert {'field': 'name', 'code': 'REQUIRED', 'message': '필수 항목입니다.'} in error['fieldErrors']
    assert 'secure-password-123' not in response.text
    response = client.post('/api/auth/login', content='{}', headers={'Content-Type': 'text/plain'})
    assert response.status_code == 415
    assert response.json()['error']['requestId'] == response.headers['x-request-id']


def test_login_and_refresh_rate_limits(db_and_client, monkeypatch):
    from app.api import auth as auth_api
    from app.services import rate_limit

    monkeypatch.setattr(rate_limit.time, 'time', lambda: 1_800_000_000)
    monkeypatch.setattr(auth_api, 'AUTH_LOGIN_RATE_LIMIT', 2)
    monkeypatch.setattr(auth_api, 'AUTH_REFRESH_RATE_LIMIT', 2)
    _, client = db_and_client
    credentials = {'loginId': 'missing', 'password': 'secure-password-123'}
    assert client.post('/api/auth/login', json=credentials).status_code == 401
    assert client.post('/api/auth/login', json=credentials).status_code == 401
    response = client.post('/api/auth/login', json=credentials)
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'RATE_LIMITED'
    assert int(response.headers['Retry-After']) > 0
    assert response.json()['error']['requestId'] == response.headers['x-request-id']
    assert client.post('/api/auth/refresh', json={'refreshToken': 'invalid'}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': 'invalid'}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': 'invalid'}).status_code == 429


def test_refresh_reuse_revokes_only_the_affected_session(db_and_client):
    _, client = db_and_client
    signup = {'loginId': 'tester', 'name': 'Test', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json=signup)
    credentials = {'loginId': signup['loginId'], 'password': signup['password']}
    first = client.post('/api/auth/login', json=credentials).json()['data']
    other = client.post('/api/auth/login', json=credentials).json()['data']
    rotated = client.post('/api/auth/refresh', json={'refreshToken': first['refreshToken']}).json()['data']
    assert client.post('/api/auth/refresh', json={'refreshToken': first['refreshToken']}).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {rotated['accessToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': rotated['refreshToken']}).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {other['accessToken']}"}).status_code == 200


def test_cleanup_expired_preserves_active_sessions(db_and_client):
    from app.maintenance import cleanup_expired
    from app.models.auth import UsedRefreshToken
    sessions, client = db_and_client
    body = {'loginId': 'tester', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json={**body, 'name': 'Test'})
    expired = client.post('/api/auth/login', json=body).json()['data']
    rotated = client.post('/api/auth/refresh', json={'refreshToken': expired['refreshToken']}).json()['data']
    active = client.post('/api/auth/login', json=body).json()['data']
    with sessions() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == digest(rotated['refreshToken'])))
        session.refresh_expires_at = now() - timedelta(seconds=1)
        db.commit()
        assert cleanup_expired(db)['sessions'] == 1
        assert db.scalar(select(UsedRefreshToken)) is None
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + active['accessToken']}).status_code == 200
