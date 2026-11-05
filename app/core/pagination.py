import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import Query
from sqlalchemy import and_, or_

from app.core.errors import ApiError

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=2048)]


def page_query(db, statement, model, scope: str, limit: int, cursor: str | None):
    """Stable descending keyset pagination, bound to the authorized parent resource."""
    if cursor:
        try:
            value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if not isinstance(value, dict) or value.get('scope') != scope or value.get('v') != 1:
                raise ValueError()
            created = datetime.fromisoformat(value['createdAt'])
            if created.tzinfo is not None or not isinstance(value['id'], str) or len(value['id']) > 100:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None
        statement = statement.where(or_(model.created_at < created,
            and_(model.created_at == created, model.id < value['id'])))
    rows = list(db.scalars(statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({'v': 1, 'scope': scope,
            'createdAt': last.created_at.replace(tzinfo=None).isoformat(), 'id': last.id}).encode()).decode()
    return rows[:limit], next_cursor
