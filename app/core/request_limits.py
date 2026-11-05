import re

from app.core.errors import error_response


class AuthRequestLimits:
    """Limit JSON auth request bodies without buffering unbounded input."""

    def __init__(self, app, max_bytes: int = 1024 * 1024):
        """Initialize the state required by this middleware or domain error."""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        """Apply this HTTP middleware while preserving non-HTTP ASGI traffic."""
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH') or not scope['path'].startswith('/api/'):
            await self.app(scope, receive, send)
            return
        headers = dict(scope['headers'])
        content_type = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
        csv_upload = scope['method'] == 'POST' and re.fullmatch(
            r'/api/projects/[A-Za-z0-9_-]+/testsets/[A-Za-z0-9_-]+/versions/[A-Za-z0-9_-]+/questions/import', scope['path'])
        upload = csv_upload or (scope['method'] == 'POST' and re.fullmatch(
            r'/api/projects/[A-Za-z0-9_-]+/policies/imports?', scope['path']))
        allowed_type = b'multipart/form-data' if upload else b'application/json'
        if content_type != allowed_type:
            await error_response(scope, 415, 'UNSUPPORTED_MEDIA_TYPE',
                'multipart/form-data 요청이 필요합니다.' if upload else 'JSON 요청이 필요합니다.')(scope, receive, send)
            return
        upload_limit = (3 if csv_upload else 51 if scope['path'].endswith('/imports') else 11) * 1024 * 1024
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > (upload_limit if upload else self.max_bytes):
                await error_response(scope, 413, 'PAYLOAD_TOO_LARGE', '요청 크기가 너무 큽니다.')(scope, receive, send)
                return
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            """Replay the bounded request body once to downstream JSON parsing."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
