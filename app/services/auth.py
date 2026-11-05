import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pwdlib import PasswordHash
from sqlalchemy import case, delete, select, update
from sqlalchemy.orm import Session

from app.core.config import ACCESS_TOKEN_MINUTES, REFRESH_TOKEN_DAYS, AUTH_SESSION_MAX_DAYS, AUTH_SESSION_MAX_REFRESHES
from app.models.auth import AuthSession, UsedRefreshToken, User

password_hash = PasswordHash.recommended()
# A real Argon2 hash keeps the unknown-user path comparable to wrong passwords.
DUMMY_HASH = password_hash.hash('osori-dummy-password-never-used')


def now() -> datetime:
    """Return the current UTC time used by token lifetime checks."""
    return datetime.now(UTC)


def digest(token: str) -> str:
    """Hash an opaque token before persistence or lookup; never store its plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """Generate a cryptographically random bearer token."""
    return secrets.token_urlsafe(32)


def user_data(user: User) -> dict:
    """Serialize public user fields and normalize persisted timestamps to UTC."""
    created = user.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {'id': user.id, 'loginId': user.login_id, 'name': user.name,
            'createdAt': created.isoformat().replace('+00:00', 'Z'),
            'lastSeenAt': user.last_seen_at.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z') if user.last_seen_at else None}


def create_session(db: Session, user: User) -> dict:
    """Persist a new token pair with a fixed maximum lifetime and no refresh history."""
    access, refresh = new_token(), new_token()
    current = now()
    user.last_seen_at = current
    absolute = current + timedelta(days=AUTH_SESSION_MAX_DAYS)
    db.add(AuthSession(id=str(uuid4()), user_id=user.id, access_hash=digest(access),
                       refresh_hash=digest(refresh),
                       absolute_expires_at=absolute, refresh_count=0,
                       access_expires_at=min(absolute, current + timedelta(minutes=ACCESS_TOKEN_MINUTES)),
                       refresh_expires_at=min(absolute, current + timedelta(days=REFRESH_TOKEN_DAYS))))
    db.commit()
    return {'accessToken': access, 'refreshToken': refresh}


def rotate(db: Session, token: str) -> dict | None:
    """Rotate once atomically, retain replay evidence, and revoke replayed or exhausted sessions."""
    access, refresh = new_token(), new_token()
    current = now()
    old_hash = digest(token)
    # The row update serializes concurrent uses of the same token in MySQL.
    result = db.execute(update(AuthSession).where(
        AuthSession.refresh_hash == old_hash, AuthSession.refresh_expires_at > current,
        AuthSession.absolute_expires_at > current, AuthSession.refresh_count < AUTH_SESSION_MAX_REFRESHES,
    ).values(access_hash=digest(access), refresh_hash=digest(refresh),
             access_expires_at=case((AuthSession.absolute_expires_at < current + timedelta(minutes=ACCESS_TOKEN_MINUTES), AuthSession.absolute_expires_at), else_=current + timedelta(minutes=ACCESS_TOKEN_MINUTES)),
             refresh_expires_at=case((AuthSession.absolute_expires_at < current + timedelta(days=REFRESH_TOKEN_DAYS), AuthSession.absolute_expires_at), else_=current + timedelta(days=REFRESH_TOKEN_DAYS)),
             refresh_count=AuthSession.refresh_count + 1))
    if result.rowcount == 1:
        session_id = db.scalar(select(AuthSession.id).where(AuthSession.refresh_hash == digest(refresh)))
        db.add(UsedRefreshToken(token_hash=old_hash, session_id=session_id))
        db.commit()
        return {'accessToken': access, 'refreshToken': refresh}

    # Expired or exhausted current tokens end the session as well.
    db.execute(delete(AuthSession).where(AuthSession.refresh_hash == old_hash))

    # A rotated token being used again may mean theft. Revoke its entire session.
    # A locking read sees the latest committed history under MySQL REPEATABLE READ.
    used = db.scalar(select(UsedRefreshToken).where(
        UsedRefreshToken.token_hash == old_hash).with_for_update())
    if used is not None:
        db.execute(delete(AuthSession).where(AuthSession.id == used.session_id))
    db.commit()
    return None


def revoke(db: Session, token: str) -> bool:
    """Delete the current login session when presented with its valid refresh token."""
    result = db.execute(delete(AuthSession).where(
        AuthSession.refresh_hash == digest(token), AuthSession.refresh_expires_at > now()))
    db.commit()
    return result.rowcount == 1


def user_from_access(db: Session, token: str) -> User | None:
    """Resolve a bearer token only while both its access and absolute session deadlines are valid."""
    user = db.scalar(select(User).join(AuthSession, AuthSession.user_id == User.id).where(
        AuthSession.access_hash == digest(token), AuthSession.access_expires_at > now(),
        AuthSession.absolute_expires_at > now()))
    if user and (user.last_seen_at is None or user.last_seen_at.replace(tzinfo=UTC) < now() - timedelta(minutes=5)):
        user.last_seen_at = now()
        db.commit()
    return user
