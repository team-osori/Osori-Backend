from datetime import timedelta

from sqlalchemy import select

from app.models.auth import AuthSession, User
from app.services.auth import digest, now


def test_auth_lifecycle(db_and_client):
    """Verify auth lifecycle against the observable API or database contract."""
    sessions, client = db_and_client
    signup = {'email': 'osori_dev@example.com', 'name': '담당자', 'password': 'secure-password-123'}
    assert client.get('/api/auth/email-availability', params={'email': signup['email']}).json()['data']['available'] is True
    created = client.post('/api/auth/signup', json=signup)
    assert created.status_code == 201
    assert created.json()['data']['email'] == 'osori_dev@example.com'
    assert 'password' not in created.json()['data']
    assert client.post('/api/auth/signup', json=signup).status_code == 409
    assert client.get('/api/auth/email-availability', params={'email': signup['email']}).json()['data']['available'] is False
    with sessions() as db:
        user = db.scalar(select(User))
        assert user.password_hash != signup['password']
    login = client.post('/api/auth/login', json={'email': signup['email'], 'password': signup['password']})
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
    """Verify errors and expiration against the observable API or database contract."""
    sessions, client = db_and_client
    assert client.post('/api/auth/signup', json={'email': 'x', 'name': 'a', 'password': 'short'}).status_code == 400
    signup = {'email': 'tester@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json=signup)
    wrong = client.post('/api/auth/login', json={'email': 'tester@example.com', 'password': 'wrong-password-123'})
    missing = client.post('/api/auth/login', json={'email': 'missing@example.com', 'password': 'wrong-password-123'})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()['error']['code'] == missing.json()['error']['code'] == 'INVALID_CREDENTIALS'
    assert wrong.json()['error']['message'] == missing.json()['error']['message']
    assert client.get('/api/auth/me').status_code == 401
    tokens = client.post('/api/auth/login', json={'email': 'tester@example.com', 'password': signup['password']}).json()['data']
    with sessions() as db:
        session = db.scalar(select(AuthSession))
        session.access_expires_at = now() - timedelta(seconds=1)
        session.refresh_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {tokens['accessToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).status_code == 401
    assert client.post('/api/auth/logout', json={'refreshToken': tokens['refreshToken']}).status_code == 401


def test_sessions_are_independent_and_invalid_tokens_are_rejected(db_and_client):
    """Verify sessions are independent and invalid tokens are rejected against the observable API or database contract."""
    _, client = db_and_client
    signup = {'email': 'tester@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=signup).status_code == 201
    credentials = {'email': signup['email'], 'password': signup['password']}
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
    """Verify signup common rules and body limits against the observable API or database contract."""
    _, client = db_and_client
    body = {'email': 'tester@example.com', 'name': '  담당자  ', 'password': ' secure-password-123 '}
    response = client.post('/api/auth/signup', json=body)
    assert response.status_code == 201
    assert response.headers['location'] == '/api/auth/me'
    assert response.json()['data']['name'] == '담당자'
    assert client.post('/api/auth/login', json={'email': 'tester@example.com', 'password': body['password']}).status_code == 200
    assert client.post('/api/auth/login', json={'email': 'tester@example.com', 'password': body['password'].strip()}).status_code == 401
    assert client.post('/api/auth/signup', json={**body, 'extra': True}).status_code == 400
    assert client.post('/api/auth/login', content='{}', headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post('/api/auth/login', content=b' ' * (1024 * 1024 + 1), headers={'Content-Type': 'application/json'}).status_code == 413


def test_email_is_normalized(db_and_client):
    """Verify email is normalized against the observable API or database contract."""
    _, client = db_and_client
    signup = {'email': 'Osori_DEV@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    response = client.post('/api/auth/signup', json=signup)
    assert response.status_code == 201
    assert response.json()['data']['email'] == 'osori_dev@example.com'
    assert client.get('/api/auth/email-availability', params={'email': 'OSORI_dev@example.com'}).json()['data'] == {'email': 'osori_dev@example.com', 'available': False}
    assert client.post('/api/auth/signup', json={**signup, 'email': 'osori_dev@example.com'}).status_code == 409
    assert client.post('/api/auth/login', json={'email': 'OSORI_DEV@example.com', 'password': signup['password']}).status_code == 200


def test_error_details_and_request_id(db_and_client):
    """Verify error details and request id against the observable API or database contract."""
    _, client = db_and_client
    response = client.post('/api/auth/signup', json={'email': 'tester@example.com', 'password': 'secure-password-123'})
    assert response.status_code == 400
    error = response.json()['error']
    assert error['requestId'] == response.headers['x-request-id']
    assert {'field': 'name', 'code': 'REQUIRED', 'message': '필수 항목입니다.'} in error['fieldErrors']
    assert 'secure-password-123' not in response.text
    response = client.post('/api/auth/login', content='{}', headers={'Content-Type': 'text/plain'})
    assert response.status_code == 415
    assert response.json()['error']['requestId'] == response.headers['x-request-id']


def test_login_and_refresh_rate_limits(db_and_client, monkeypatch):
    """Verify login and refresh rate limits against the observable API or database contract."""
    from app.api import auth as auth_api
    from app.services import rate_limit

    monkeypatch.setattr(rate_limit.time, 'time', lambda: 1_800_000_000)
    monkeypatch.setattr(auth_api, 'AUTH_LOGIN_RATE_LIMIT', 2)
    monkeypatch.setattr(auth_api, 'AUTH_REFRESH_RATE_LIMIT', 2)
    _, client = db_and_client
    credentials = {'email': 'missing@example.com', 'password': 'secure-password-123'}
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
    """Verify refresh reuse revokes only the affected session against the observable API or database contract."""
    _, client = db_and_client
    signup = {'email': 'tester@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json=signup)
    credentials = {'email': signup['email'], 'password': signup['password']}
    first = client.post('/api/auth/login', json=credentials).json()['data']
    other = client.post('/api/auth/login', json=credentials).json()['data']
    rotated = client.post('/api/auth/refresh', json={'refreshToken': first['refreshToken']}).json()['data']
    assert client.post('/api/auth/refresh', json={'refreshToken': first['refreshToken']}).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {rotated['accessToken']}"}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': rotated['refreshToken']}).status_code == 401
    assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {other['accessToken']}"}).status_code == 200


def test_cleanup_expired_preserves_active_sessions(db_and_client):
    """Verify cleanup expired preserves active sessions against the observable API or database contract."""
    from app.maintenance import cleanup_expired
    from app.models.auth import UsedRefreshToken
    sessions, client = db_and_client
    body = {'email': 'tester@example.com', 'password': 'secure-password-123'}
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


def test_signup_throttles_before_hashing_or_creating_user(db_and_client, monkeypatch):
    """A denied signup must not incur password hashing or create another account."""
    from app.api import auth as auth_api
    from app.services import auth
    monkeypatch.setattr(auth_api, 'AUTH_SIGNUP_RATE_LIMIT', 1)
    sessions, client = db_and_client
    body = {'email': 'first_user@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=body).status_code == 201

    def unexpected_hash(*args, **kwargs):
        """Fail if a throttled request reaches expensive password hashing."""
        raise AssertionError('Hashing must not run after the signup budget is exhausted')

    monkeypatch.setattr(auth.password_hash, 'hash', unexpected_hash)
    response = client.post('/api/auth/signup', json={**body, 'email': 'second_user@example.com'})
    assert response.status_code == 429
    assert int(response.headers['retry-after']) > 0
    with sessions() as db:
        assert db.scalar(select(User).where(User.email == 'second_user@example.com')) is None


def test_absolute_session_expiry_and_refresh_budget(db_and_client, monkeypatch):
    """Repeated refreshes cannot extend a session forever or retain unlimited history."""
    from datetime import UTC, datetime
    from app.services import auth
    from app.models.auth import UsedRefreshToken
    sessions, client = db_and_client
    current = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(auth, 'now', lambda: current)
    monkeypatch.setattr(auth, 'AUTH_SESSION_MAX_DAYS', 1)
    monkeypatch.setattr(auth, 'AUTH_SESSION_MAX_REFRESHES', 1)
    body = {'email': 'tester@example.com', 'name': 'Test', 'password': 'secure-password-123'}
    client.post('/api/auth/signup', json=body)
    credentials = {'email': body['email'], 'password': body['password']}
    tokens = client.post('/api/auth/login', json=credentials).json()['data']
    rotated = client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).json()['data']
    with sessions() as db:
        session = db.scalar(select(AuthSession))
        assert session.refresh_count == 1
        assert session.refresh_expires_at.replace(tzinfo=UTC) == current + timedelta(days=1)
    assert client.post('/api/auth/refresh', json={'refreshToken': rotated['refreshToken']}).status_code == 401
    with sessions() as db:
        assert db.scalar(select(UsedRefreshToken)) is None
    tokens = client.post('/api/auth/login', json=credentials).json()['data']
    monkeypatch.setattr(auth, 'now', lambda: current + timedelta(days=1))
    assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + tokens['accessToken']}).status_code == 401
    assert client.post('/api/auth/refresh', json={'refreshToken': tokens['refreshToken']}).status_code == 401


def test_email_validation_and_legacy_id_contract(db_and_client):
    """Reject invalid identities and normalize valid email addresses."""
    _, client = db_and_client
    password = 'secure-password-123'
    for email in ('a..b@example.com', '.a@example.com', 'user@-example.com', 'user@example', 'u ser@example.com', '사용자@example.com'):
        assert client.post('/api/auth/signup', json={'email': email, 'name': 'Test', 'password': password}).status_code == 400
    assert client.post('/api/auth/signup', json={'loginId': 'legacy', 'name': 'Test', 'password': password}).status_code == 400
    assert client.post('/api/auth/login', json={'loginId': 'legacy', 'password': password}).status_code == 400
    response = client.post('/api/auth/signup', json={'email': ' QA@Example.COM ', 'name': 'Test', 'password': password})
    assert response.status_code == 201
    assert response.json()['data']['email'] == 'qa@example.com'
    duplicate = client.post('/api/auth/signup', json={'email': 'qa@example.com', 'name': 'Test', 'password': password})
    assert duplicate.status_code == 409 and duplicate.json()['error']['code'] == 'EMAIL_ALREADY_EXISTS'
    assert client.post('/api/auth/login', json={'email': 'QA@EXAMPLE.COM', 'password': password}).status_code == 200


def test_legacy_email_link_preserves_projects_and_checks_original_credentials(db_and_client):
    """A legacy account keeps its identity and projects when an email is attached once."""
    from app.models.project import Project
    from app.services.auth import password_hash
    sessions, client = db_and_client
    with sessions() as db:
        db.add(User(id='legacy-user', login_id='legacy_id', email=None, name='Legacy',
                    password_hash=password_hash.hash('secure-password-123'), created_at=now()))
        db.add(Project(id='legacy-project', owner_id='legacy-user', name='Project', revision=1,
                       created_at=now(), updated_at=now()))
        db.commit()
    body = {'loginId': 'LEGACY_ID', 'password': 'secure-password-123', 'email': 'Legacy@Example.com'}
    wrong = client.post('/api/auth/legacy-email-link', json={**body, 'password': 'wrong-password-123'})
    assert wrong.status_code == 401
    with sessions() as db:
        assert db.get(User, 'legacy-user').email is None
    linked = client.post('/api/auth/legacy-email-link', json=body)
    assert linked.status_code == 200 and linked.json()['data']['id'] == 'legacy-user'
    assert linked.json()['data']['email'] == 'legacy@example.com'
    assert client.post('/api/auth/legacy-email-link', json=body).status_code == 401
    login = client.post('/api/auth/login', json={'email': 'legacy@example.com', 'password': body['password']})
    assert login.status_code == 200
    headers = {'Authorization': 'Bearer ' + login.json()['data']['accessToken']}
    assert client.get('/api/projects/legacy-project', headers=headers).status_code == 200


def test_legacy_email_link_rejects_an_existing_email(db_and_client):
    """An existing email cannot be reassigned to a legacy account."""
    from app.services.auth import password_hash
    sessions, client = db_and_client
    body = {'email': 'claimed@example.com', 'name': 'Existing', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=body).status_code == 201
    with sessions() as db:
        db.add(User(id='legacy', login_id='legacy_id', email=None, name='Legacy',
                    password_hash=password_hash.hash(body['password']), created_at=now()))
        db.commit()
    response = client.post('/api/auth/legacy-email-link', json={'loginId': 'legacy_id',
        'password': body['password'], 'email': body['email']})
    assert response.status_code == 409 and response.json()['error']['code'] == 'EMAIL_ALREADY_EXISTS'
    with sessions() as db:
        assert db.get(User, 'legacy').email is None
