import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pwdlib import PasswordHash
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import ACCESS_TOKEN_MINUTES, REFRESH_TOKEN_DAYS
from app.models.auth import AuthSession, UsedRefreshToken, User

password_hash = PasswordHash.recommended()
# A real Argon2 hash keeps the unknown-user path comparable to wrong passwords.
DUMMY_HASH = password_hash.hash('osori-dummy-password-never-used')


def now() -> datetime:
    return datetime.now(UTC)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def user_data(user: User) -> dict:
    created = user.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {'id': user.id, 'loginId': user.login_id, 'name': user.name,
            'createdAt': created.isoformat().replace('+00:00', 'Z')}


def create_session(db: Session, user: User) -> dict:
    access, refresh = new_token(), new_token()
    current = now()
    db.add(AuthSession(id=str(uuid4()), user_id=user.id, access_hash=digest(access),
                       refresh_hash=digest(refresh),
                       access_expires_at=current + timedelta(minutes=ACCESS_TOKEN_MINUTES),
                       refresh_expires_at=current + timedelta(days=REFRESH_TOKEN_DAYS)))
    db.commit()
    return {'accessToken': access, 'refreshToken': refresh}


def rotate(db: Session, token: str) -> dict | None:
    access, refresh = new_token(), new_token()
    current = now()
    old_hash = digest(token)
    # The row update serializes concurrent uses of the same token in MySQL.
    result = db.execute(update(AuthSession).where(
        AuthSession.refresh_hash == old_hash, AuthSession.refresh_expires_at > current,
    ).values(access_hash=digest(access), refresh_hash=digest(refresh),
             access_expires_at=current + timedelta(minutes=ACCESS_TOKEN_MINUTES),
             refresh_expires_at=current + timedelta(days=REFRESH_TOKEN_DAYS)))
    if result.rowcount == 1:
        session_id = db.scalar(select(AuthSession.id).where(AuthSession.refresh_hash == digest(refresh)))
        db.add(UsedRefreshToken(token_hash=old_hash, session_id=session_id))
        db.commit()
        return {'accessToken': access, 'refreshToken': refresh}

    # A rotated token being used again may mean theft. Revoke its entire session.
    # A locking read sees the latest committed history under MySQL REPEATABLE READ.
    used = db.scalar(select(UsedRefreshToken).where(
        UsedRefreshToken.token_hash == old_hash).with_for_update())
    if used is not None:
        db.execute(delete(AuthSession).where(AuthSession.id == used.session_id))
    db.commit()
    return None


def revoke(db: Session, token: str) -> bool:
    result = db.execute(delete(AuthSession).where(
        AuthSession.refresh_hash == digest(token), AuthSession.refresh_expires_at > now()))
    db.commit()
    return result.rowcount == 1


def user_from_access(db: Session, token: str) -> User | None:
    return db.scalar(select(User).join(AuthSession, AuthSession.user_id == User.id).where(
        AuthSession.access_hash == digest(token), AuthSession.access_expires_at > now()))
