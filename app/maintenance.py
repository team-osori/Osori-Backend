"""Run with: python -m app.maintenance (uses DATABASE_URL)."""
from sqlalchemy import delete

from app.core.database import SessionLocal
from app.models.auth import AuthSession
from app.models.rate_limit import AuthRateWindow
from app.services.auth import now


def cleanup_expired(db):
    current = now()
    sessions = db.execute(delete(AuthSession).where(AuthSession.refresh_expires_at <= current)).rowcount
    counters = db.execute(delete(AuthRateWindow).where(AuthRateWindow.window_start < int(current.timestamp()) - 86400)).rowcount
    db.commit()
    return {'sessions': sessions, 'rateWindows': counters}


if __name__ == '__main__':
    with SessionLocal() as db:
        print(cleanup_expired(db))
