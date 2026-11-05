"""Test helpers that exercise email verification without sending real mail."""
from app.schemas.auth import normalize_email

captured_codes = {}


def verified_post(client, path, *, json):
    """Issue a real challenge and submit the code captured by the test mailer."""
    body = dict(json)
    try:
        email = normalize_email(body.get('email', ''))
    except (ValueError, AttributeError):
        return client.post(path, json=body)
    response = client.post('/api/auth/email-verifications', json={'email': email})
    if response.status_code != 202:
        return response
    body['verificationId'] = response.json()['data']['verificationId']
    body['verificationCode'] = captured_codes[email]
    return client.post(path, json=body)
