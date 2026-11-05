"""Validate untrusted cursor JSON before passing any value to a database driver."""
import base64
from datetime import datetime
import json
import re

from app.core.errors import ApiError


def unique_object(pairs):
    """Reject duplicate fields instead of silently accepting the last JSON value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate cursor field')
        result[key] = value
    return result


def reject_constant(value):
    """Reject JSON extensions such as NaN and Infinity."""
    raise ValueError('Non-finite cursor constant')


def decode_cursor(cursor, fields):
    """Decode a bounded envelope with exactly the fields emitted by its endpoint."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True),
            object_pairs_hook=unique_object, parse_constant=reject_constant)
        if type(value) is not dict or set(value) != set(fields):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None


def cursor_id(value):
    """Allow only the ASCII identifiers supported by API paths, excluding lone surrogates."""
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) is None:
        raise ValueError('Invalid cursor identifier')
    return value


def cursor_time(value):
    """Require canonical naive UTC timestamps within MySQL DATETIME's domain."""
    if not isinstance(value, str):
        raise ValueError('Invalid cursor timestamp')
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is not None or instant.year < 1000 or instant.isoformat() != value:
        raise ValueError('Invalid cursor timestamp')
    return instant


def cursor_integer(value):
    """Reject coercion, booleans and values outside the stored signed INT version domain."""
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ValueError('Invalid cursor integer')
    return value
