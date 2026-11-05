import asyncio
from copy import deepcopy
import socket

import aiohttp
import pytest

from app.core.errors import ApiError
from app.services import chatbot_gateway as gateway
from app.services.json_pointer import read_pointer, write_pointer
from test_connections_prompts import CONFIG


@pytest.mark.parametrize('url', [
    'http://example.com/chat', 'https://localhost/chat', 'https://127.0.0.1/',
    'https://10.0.0.1/', 'https://169.254.169.254/', 'https://[::1]/',
    'https://[::ffff:127.0.0.1]/', 'https://192.168.1.10/', 'https://100.64.0.1/',
    'https://224.0.0.1/', 'https://user:password@example.com/', 'https://example.com:8080/',
    'https://2130706433/', 'https://127.1/', 'https://0177.0.0.1/',
    'https://example.com/#fragment', 'https://example.com/\r\nHost:localhost',
    'https://example.com\\@127.0.0.1/', 'https://[fe80::1%25en0]/',
])
def test_unsafe_endpoints(url):
    with pytest.raises(ApiError) as error:
        gateway.validate_endpoint(url)
    assert error.value.code == 'UNSAFE_ENDPOINT'


def test_dns_public_private_mixture_is_rejected_and_result_is_pinned(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]

        async def lookup(*args, **kwargs):
            return records

        monkeypatch.setattr(loop, 'getaddrinfo', lookup)
        resolver = gateway.PublicResolver()
        result = await resolver.resolve('chatbot.example.com', 443)
        assert result[0]['host'] == '93.184.216.34' and result[0]['flags'] == socket.AI_NUMERICHOST
        records.append((socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)))
        with pytest.raises(ApiError):
            await resolver.resolve('chatbot.example.com', 443)
    asyncio.run(check())


def test_pointer_escaping_arrays_and_no_template_mutation():
    document = {'a/b': {'~key': ['old']}}
    write_pointer(document, '/a~1b/~0key/0', 'new')
    assert read_pointer(document, '/a~1b/~0key/0') == 'new'
    for pointer in ('/a~1b/~0key/-1', '/a~1b/~0key/00', '/a~1b/~0key/1', '/bad~x'):
        with pytest.raises(ValueError):
            read_pointer(document, pointer)
    config = deepcopy(CONFIG)
    sent = []

    async def sender(_, payload, headers):
        sent.append(deepcopy(payload))
        payload['messages'][0]['content'] = 'mutated by transport'
        return 'answer'

    asyncio.run(gateway.execute_chatbot(config, 'Q', 'A', 'secret', sender=sender))
    asyncio.run(gateway.execute_chatbot(config, 'Q', 'B', 'secret', sender=sender))
    assert sent[0]['messages'][0]['content'] == 'A'
    assert sent[1]['messages'][0]['content'] == 'B'
    assert config['requestTemplate'] == CONFIG['requestTemplate']


@pytest.mark.parametrize('safe,retries,expected', [(False, 2, 1), (True, 2, 3), (True, 0, 1)])
def test_only_safe_requests_are_retried(safe, retries, expected):
    config = {**CONFIG, 'retrySafe': safe, 'maxRetries': retries}

    async def sender(*args):
        raise aiohttp.ClientConnectionError('sensitive-host-and-token')

    result = asyncio.run(gateway.execute_chatbot(config, 'Q', 'P', 'secret', sender=sender))
    assert result.attempt_count == expected
    assert result.error['code'] == 'UPSTREAM_CONNECTION_FAILED'
    assert 'sensitive' not in str(result)


def test_auth_failure_is_not_retried_and_total_timeout_is_enforced():
    async def failed(*args):
        raise gateway.GatewayError('UPSTREAM_AUTH_FAILED', '인증 실패')

    result = asyncio.run(gateway.execute_chatbot({**CONFIG, 'retrySafe': True, 'maxRetries': 2},
                                                 'Q', 'P', 'secret', sender=failed))
    assert result.attempt_count == 1

    async def slow(*args):
        await asyncio.sleep(10)

    result = asyncio.run(gateway.execute_chatbot({**CONFIG, 'timeoutMs': 10},
                                                 'Q', 'P', 'secret', sender=slow))
    assert result.error['code'] == 'UPSTREAM_TIMEOUT' and result.latency_ms < 1000


@pytest.mark.parametrize('status,raw,encoding,code', [
    (401, b'leaked-secret', None, 'UPSTREAM_AUTH_FAILED'),
    (403, b'', None, 'UPSTREAM_AUTH_FAILED'),
    (429, b'', None, 'UPSTREAM_RATE_LIMITED'),
    (500, b'', None, 'UPSTREAM_HTTP_ERROR'),
    (302, b'', None, 'UPSTREAM_HTTP_ERROR'),
    (200, b'not json', None, 'INVALID_UPSTREAM_RESPONSE'),
    (200, b'{"answer": 123}', None, 'INVALID_UPSTREAM_RESPONSE'),
    (200, b'{"answer": "  "}', None, 'INVALID_UPSTREAM_RESPONSE'),
    (200, b'{"wrong": "answer"}', None, 'INVALID_UPSTREAM_RESPONSE'),
    (200, b'x' * (1024 * 1024 + 1), None, 'UPSTREAM_RESPONSE_TOO_LARGE'),
    (200, b'compressed', 'gzip', 'INVALID_UPSTREAM_RESPONSE'),
    (200, b'{"answer":"ok"}', None, None),
])
def test_http_transport_status_parsing_size_and_redirects(monkeypatch, status, raw, encoding, code):
    class Content:
        async def iter_chunked(self, size):
            for i in range(0, len(raw), size):
                yield raw[i:i + size]

    class Response:
        content = Content()
        headers = {'Content-Encoding': encoding} if encoding else {}

        async def __aenter__(self):
            self.status = status
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['auto_decompress'] is False
            self.connector = kwargs['connector']

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def post(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            return Response()

    monkeypatch.setattr(gateway.aiohttp, 'ClientSession', Session)
    result = asyncio.run(gateway.execute_chatbot(CONFIG, 'Q', 'P', 'secret'))
    if code:
        assert result.error['code'] == code and result.answer is None
    else:
        assert result.answer == 'ok' and result.error is None


def test_header_auth_and_connection_only():
    config = {**CONFIG, 'executionMode': 'CONNECTION_ONLY', 'promptPointer': None,
              'auth': {'type': 'HEADER', 'headerName': 'X-Api-Key'}}

    async def sender(_, payload, headers):
        assert headers['X-Api-Key'] == 'secret'
        assert 'Authorization' not in headers
        assert payload['messages'][0]['content'] == ''
        return 'answer'

    assert asyncio.run(gateway.execute_chatbot(config, 'Q', None, 'secret', sender=sender)).answer == 'answer'
