"""Email ownership must be proven before identities or invitations are claimed."""
from datetime import timedelta

from sqlalchemy import select

from app.models.auth import EmailVerification, User
from app.models.project import Project
from app.services.auth import now, password_hash
from app.services.email_verification import send_code as real_send_code
from email_helpers import captured_codes


def challenge(client, address):
    response = client.post('/api/auth/email-verifications', json={'email': address})
    assert response.status_code == 202
    assert captured_codes[address] not in response.text
    return {'verificationId': response.json()['data']['verificationId'], 'verificationCode': captured_codes[address]}


def test_signup_requires_correct_unexpired_single_use_challenge(db_and_client):
    factory, client = db_and_client
    address = 'victim@example.com'
    data = {'email': address, 'name': 'Victim', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=data).status_code == 400
    proof = challenge(client, address)
    incorrect = {**proof, 'verificationCode': '000000' if proof['verificationCode'] != '000000' else '000001'}
    assert client.post('/api/auth/signup', json={**data, **incorrect}).status_code == 400
    assert client.post('/api/auth/signup', json={**data, **proof, 'email': 'attacker@example.com'}).status_code == 400
    assert client.post('/api/auth/signup', json={**data, **proof}).status_code == 201
    assert client.post('/api/auth/signup', json={**data, **proof}).status_code == 400
    with factory() as db:
        assert db.scalar(select(User).where(User.email == 'attacker@example.com')) is None
        row = db.get(EmailVerification, proof['verificationId'])
        assert row.code_hash != proof['verificationCode'] and row.consumed_at is not None


def test_attempt_limit_and_expiry(db_and_client):
    factory, client = db_and_client
    address = 'limit@example.com'
    data = {'email': address, 'name': 'Test', 'password': 'secure-password-123'}
    proof = challenge(client, address)
    bad = {**proof, 'verificationCode': '000000' if proof['verificationCode'] != '000000' else '000001'}
    for _ in range(5):
        assert client.post('/api/auth/signup', json={**data, **bad}).status_code == 400
    assert client.post('/api/auth/signup', json={**data, **proof}).status_code == 400
    proof = challenge(client, address)
    with factory() as db:
        row = db.get(EmailVerification, proof['verificationId'])
        row.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post('/api/auth/signup', json={**data, **proof}).status_code == 400


def test_legacy_email_link_requires_original_password_and_proof(db_and_client):
    factory, client = db_and_client
    with factory() as db:
        db.add(User(id='legacy-user', login_id='legacy_id', email=None, name='Legacy',
                    password_hash=password_hash.hash('secure-password-123'), created_at=now()))
        db.add(Project(id='legacy-project', owner_id='legacy-user', name='Project', revision=1, created_at=now(), updated_at=now()))
        db.commit()
    address = 'legacy@example.com'
    proof = challenge(client, address)
    body = {'loginId': 'legacy_id', 'password': 'secure-password-123', 'email': address, **proof}
    assert client.post('/api/auth/legacy-email-link', json={**body, 'password': 'incorrect-password'}).status_code == 401
    assert client.post('/api/auth/legacy-email-link', json={**body, 'email': 'attacker@example.com'}).status_code == 400
    result = client.post('/api/auth/legacy-email-link', json=body)
    assert result.status_code == 200 and result.json()['data']['id'] == 'legacy-user'
    login = client.post('/api/auth/login', json={'email': address, 'password': body['password']})
    assert login.status_code == 200
    headers = {'Authorization': 'Bearer ' + login.json()['data']['accessToken']}
    assert client.get('/api/projects/legacy-project', headers=headers).status_code == 200
    assert client.post('/api/auth/legacy-email-link', json=body).status_code == 401


def test_mail_delivery_failure_removes_challenge_and_does_not_create_account(db_and_client, monkeypatch):
    from app.core.errors import ApiError
    from app.services import email_verification
    factory, client = db_and_client
    def fail(email, code):
        raise ApiError(503, 'EMAIL_DELIVERY_FAILED', '발송 실패')
    monkeypatch.setattr(email_verification, 'send_code', fail)
    result = client.post('/api/auth/email-verifications', json={'email': 'failed@example.com'})
    assert result.status_code == 503
    with factory() as db:
        assert db.scalar(select(EmailVerification)) is None
        assert db.scalar(select(User)) is None


def test_unverified_email_cannot_receive_project_invitation(db_and_client):
    from test_projects import register
    _, client = db_and_client
    owner = register(client, 'owner')
    project = client.post('/api/projects', headers=owner, json={'name': 'Project'}).headers['location']
    body = {'email': 'victim@example.com', 'name': 'Impostor', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json=body).status_code == 400
    assert client.post(project + '/invitations', headers=owner, json={'email': body['email']}).status_code == 404


def test_smtp_delivery_uses_tls_and_authentication(monkeypatch):
    """The SMTP adapter must negotiate TLS before login and sending."""
    import ssl
    from app.services import email_verification
    calls = []
    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(('connect', host, port, timeout))
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ehlo(self): calls.append(('ehlo',))
        def starttls(self, context):
            assert isinstance(context, ssl.SSLContext) and context.check_hostname
            calls.append(('starttls',))
        def login(self, username, password): calls.append(('login', username, password))
        def send_message(self, message): calls.append(('send', message['To'], message.get_content()))
    monkeypatch.setenv('SMTP_HOST', 'smtp.example.com')
    monkeypatch.setenv('SMTP_PORT', '587')
    monkeypatch.setenv('SMTP_FROM', 'service@example.com')
    monkeypatch.setenv('SMTP_USERNAME', 'service')
    monkeypatch.setenv('SMTP_PASSWORD', 'test-password')
    monkeypatch.setattr(email_verification.smtplib, 'SMTP', SMTP)
    real_send_code('recipient@example.com', '123456')
    assert [call[0] for call in calls] == ['connect', 'ehlo', 'starttls', 'ehlo', 'login', 'send']
    assert calls[-1][1] == 'recipient@example.com' and '123456' in calls[-1][2]


def test_missing_signing_secret_fails_closed(db_and_client, monkeypatch):
    """A misconfigured deployment must not issue a usable challenge."""
    factory, client = db_and_client
    monkeypatch.delenv('EMAIL_VERIFICATION_SECRET', raising=False)
    response = client.post('/api/auth/email-verifications', json={'email': 'user@example.com'})
    assert response.status_code == 503 and response.json()['error']['code'] == 'EMAIL_VERIFICATION_UNAVAILABLE'
    with factory() as db:
        assert db.scalar(select(EmailVerification)) is None


def test_email_code_delivery_is_limited_by_recipient(db_and_client, monkeypatch):
    """Repeated sends to the same address stop after the configured limit."""
    from app.api import auth as auth_api
    _, client = db_and_client
    monkeypatch.setattr(auth_api, 'AUTH_EMAIL_VERIFICATION_ADDRESS_RATE_LIMIT', 2)
    for _ in range(2):
        assert client.post('/api/auth/email-verifications', json={'email': 'user@example.com'}).status_code == 202
    limited = client.post('/api/auth/email-verifications', json={'email': 'USER@example.com'})
    assert limited.status_code == 429 and int(limited.headers['Retry-After']) > 0
