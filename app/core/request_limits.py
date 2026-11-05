from app.core.errors import error_response


class AuthRequestLimits:
    """Limit JSON auth request bodies without buffering unbounded input."""

    def __init__(self, app, max_bytes: int = 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH') or not scope['path'].startswith('/api/'):
            await self.app(scope, receive, send)
            return
        headers = dict(scope['headers'])
        content_type = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
        if content_type != b'application/json':
            await error_response(scope, 415, 'UNSUPPORTED_MEDIA_TYPE', 'JSON 요청이 필요합니다.')(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > self.max_bytes:
                await error_response(scope, 413, 'PAYLOAD_TOO_LARGE', '요청 크기가 너무 큽니다.')(scope, receive, send)
                return
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
