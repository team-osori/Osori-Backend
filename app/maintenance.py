"""Run with: python -m app.maintenance (uses DATABASE_URL)."""
from sqlalchemy import delete, or_

from app.core.database import SessionLocal
from app.models.auth import AuthSession
from app.models.rate_limit import AuthRateWindow
from app.models.policy import GenerationRequest
from app.services.auth import now


def cleanup_expired(db):
    """Remove expired sessions, token history, rate counters and generation replay records."""
    current = now()
    sessions = db.execute(delete(AuthSession).where(or_(AuthSession.refresh_expires_at <= current, AuthSession.absolute_expires_at <= current))).rowcount
    counters = db.execute(delete(AuthRateWindow).where(AuthRateWindow.window_start < int(current.timestamp()) - 86400)).rowcount
    generations = db.execute(delete(GenerationRequest).where(GenerationRequest.expires_at <= current)).rowcount
    db.commit()
    return {'sessions': sessions, 'rateWindows': counters, 'generationRequests': generations}


if __name__ == '__main__':
    with SessionLocal() as db:
        print(cleanup_expired(db))
