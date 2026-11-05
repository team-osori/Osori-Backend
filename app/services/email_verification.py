"""Issue and consume short-lived, single-use email ownership challenges."""
import hashlib
import hmac
import os
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy import update

from app.core.errors import ApiError
from app.models.auth import EmailVerification
from app.services.auth import now


def _secret():
    """Fail closed when no strong shared verification secret is configured."""
    secret = os.getenv('EMAIL_VERIFICATION_SECRET', '')
    if len(secret.encode()) < 32:
        raise ApiError(503, 'EMAIL_VERIFICATION_UNAVAILABLE', '이메일 인증 설정이 필요합니다.')
    return secret.encode()


def _digest(challenge_id, email, code):
    """Bind the code to its challenge and email using a server-side key."""
    return hmac.new(_secret(), f'{challenge_id}|{email}|{code}'.encode(), hashlib.sha256).hexdigest()


def send_code(email, code):
    """Send the code over authenticated SMTP with certificate-validated STARTTLS."""
    host, sender = os.getenv('SMTP_HOST'), os.getenv('SMTP_FROM')
    username, password = os.getenv('SMTP_USERNAME'), os.getenv('SMTP_PASSWORD')
    if not host or not sender or not username or not password:
        raise ApiError(503, 'EMAIL_VERIFICATION_UNAVAILABLE', '이메일 발송 설정이 필요합니다.')
    message = EmailMessage()
    message['From'], message['To'] = sender, email
    message['Subject'] = '이메일 인증 코드'
    message.set_content(f'인증 코드: {code}\n10분 이내에 입력해 주세요.')
    try:
        with smtplib.SMTP(host, int(os.getenv('SMTP_PORT', '587')), timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, ValueError, smtplib.SMTPException):
        raise ApiError(503, 'EMAIL_DELIVERY_FAILED', '인증 메일을 보낼 수 없습니다.') from None


def issue(db, email):
    """Create a challenge and remove it if delivery fails."""
    challenge_id, code = str(uuid4()), f'{secrets.randbelow(1_000_000):06d}'
    row = EmailVerification(id=challenge_id, email=email, code_hash=_digest(challenge_id, email, code),
        expires_at=now() + timedelta(minutes=10), attempts=0)
    db.add(row)
    db.commit()
    try:
        send_code(email, code)
    except Exception:
        db.delete(row)
        db.commit()
        raise
    return challenge_id


def consume(db, challenge_id, email, code):
    """Atomically use one valid code or count a failed attempt; caller commits success."""
    current = now()
    condition = (EmailVerification.id == challenge_id, EmailVerification.email == email,
                 EmailVerification.expires_at > current, EmailVerification.consumed_at.is_(None),
                 EmailVerification.attempts < 5)
    digest = _digest(challenge_id, email, code)
    used = db.execute(update(EmailVerification).where(*condition, EmailVerification.code_hash == digest)
        .values(consumed_at=current).execution_options(synchronize_session=False))
    if used.rowcount != 1:
        db.execute(update(EmailVerification).where(*condition)
            .values(attempts=EmailVerification.attempts + 1).execution_options(synchronize_session=False))
        db.commit()
        raise ApiError(400, 'INVALID_EMAIL_VERIFICATION', '인증 코드가 잘못되었거나 만료되었습니다.')
