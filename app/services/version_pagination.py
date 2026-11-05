import base64
import json

from sqlalchemy import select

from app.core.errors import ApiError
from app.core.cursors import cursor_integer, decode_cursor
from app.models.prompt import PromptVersion


def version_page(db, prompt_id, limit, cursor):
    statement = select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
    if cursor:
        try:
            data = decode_cursor(cursor, {'promptId', 'before'})
            if data['promptId'] != prompt_id:
                raise ValueError()
            cursor_integer(data['before'])
        except (ValueError, TypeError, UnicodeError):
            raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None
        statement = statement.where(PromptVersion.version < data['before'])
    rows = list(db.scalars(statement.order_by(PromptVersion.version.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps({'promptId': prompt_id,
            'before': rows[limit - 1].version}).encode()).decode()
    return rows[:limit], next_cursor
