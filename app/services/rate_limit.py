import hashlib
import secrets
import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import AUTH_RATE_WINDOW_SECONDS
from app.models.rate_limit import AuthRateWindow


def check_rate_limit(db: Session, client: str, route: str, limit: int) -> int | None:
    """Return seconds until retry when this client's fixed window is exhausted."""
    current = int(time.time())
    start = current - current % AUTH_RATE_WINDOW_SECONDS
    key = hashlib.sha256(f'{client}|{route}|{start}'.encode()).hexdigest()
    values = {'key': key, 'window_start': start, 'count': 1}
    dialect = db.get_bind().dialect.name
    if dialect == 'mysql':
        statement = mysql_insert(AuthRateWindow).values(**values)
        statement = statement.on_duplicate_key_update(count=AuthRateWindow.count + 1)
    elif dialect == 'sqlite':
        statement = sqlite_insert(AuthRateWindow).values(**values)
        statement = statement.on_conflict_do_update(index_elements=['key'], set_={'count': AuthRateWindow.count + 1})
    else:
        raise RuntimeError(f'Unsupported rate-limit database: {dialect}')
    db.execute(statement)
    count = db.scalar(select(AuthRateWindow.count).where(AuthRateWindow.key == key))
    # Periodically discard expired counters. The table also works across app workers.
    if secrets.randbelow(1000) == 0:
        db.execute(delete(AuthRateWindow).where(AuthRateWindow.window_start < start - AUTH_RATE_WINDOW_SECONDS))
    db.commit()
    return start + AUTH_RATE_WINDOW_SECONDS - current if count > limit else None
