import logging
from uuid import uuid4

from starlette.responses import JSONResponse


def error_response(scope, status, code, message, *, field_errors=None, headers=None):
    """Build a sanitized error envelope with the request correlation identifier."""
    request_id = scope.setdefault('state', {}).setdefault('request_id', f'req_{uuid4().hex}')
    error = {'code': code, 'message': message, 'requestId': request_id}
    if field_errors is not None:
        error['fieldErrors'] = field_errors
    return JSONResponse({'error': error}, status_code=status, headers=headers)


class RequestIdMiddleware:
    def __init__(self, app):
        """Initialize the state required by this middleware or domain error."""
        self.app = app

    async def __call__(self, scope, receive, send):
        """Apply this HTTP middleware while preserving non-HTTP ASGI traffic."""
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request_id = f'req_{uuid4().hex}'
        scope.setdefault('state', {})['request_id'] = request_id

        started = False

        async def send_with_id(message):
            """Attach the generated request identifier before the response starts."""
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                message.setdefault('headers', []).append((b'x-request-id', request_id.encode('ascii')))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            logging.getLogger(__name__).error('Request failed requestId=%s exceptionType=%s', request_id, type(exc).__name__)
            if started:
                raise
            await error_response(scope, 500, 'INTERNAL_ERROR', '내부 오류가 발생했습니다.')(scope, receive, send_with_id)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        """Initialize the state required by this middleware or domain error."""
        self.status, self.code, self.message, self.headers = status, code, message, headers
