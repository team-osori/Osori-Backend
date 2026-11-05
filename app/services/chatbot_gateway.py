"""Isolated HTTPS calls shared by connection tests and the evaluation worker."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import ipaddress
import json
import re
import socket
from time import monotonic
from urllib.parse import urlsplit

import aiohttp

from app.core.errors import ApiError
from app.schemas.evaluation import Evidence

from app.services.json_pointer import read_pointer, write_pointer

MAX_RESPONSE_BYTES = 1024 * 1024


def public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not (isinstance(ip, ipaddress.IPv6Address)
                     and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)))


def validate_endpoint(url: str):
    """Validate syntax immediately; validate DNS again at the actual socket connection."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or parts.port not in (None, 443)
                or any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url or '%' in parts.hostname):
            raise ValueError()
        host = parts.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if (host.replace('.', '').isdigit()
                    or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)):
                raise ValueError()
        else:
            if not public_ip(str(ip)):
                raise ValueError()
        return parts
    except ValueError:
        raise ApiError(400, 'UNSAFE_ENDPOINT', '공개 HTTPS 엔드포인트(443 포트)만 사용할 수 있습니다.') from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM)
        if not records or any(not public_ip(record[4][0]) for record in records):
            raise ApiError(400, 'UNSAFE_ENDPOINT', '내부 네트워크 주소에 연결할 수 없습니다.')
        # The connector consumes these exact addresses, preventing a second DNS lookup.
        return [{'hostname': host, 'host': record[4][0], 'port': port,
                 'family': record[0], 'proto': record[2], 'flags': socket.AI_NUMERICHOST}
                for record in records]

    async def close(self):
        pass


class GatewayError(Exception):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class GatewayResult:
    answer: str | None
    latency_ms: int
    attempt_count: int
    error: dict | None
    evidence: dict | None = None


@dataclass(frozen=True)
class ChatbotReply:
    """Optional structured response while preserving existing string-only senders."""
    answer: str
    evidence: dict


async def send_once(config: dict, payload: dict, headers: dict) -> str | ChatbotReply:
    validate_endpoint(config['url'])
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=config['timeoutMs'] / 1000)) as client:
        async with client.post(config['url'], json=payload, headers=headers,
                               allow_redirects=False) as response:
            if response.status in (401, 403):
                raise GatewayError('UPSTREAM_AUTH_FAILED', '기업 챗봇 인증에 실패했습니다.')
            if response.status == 429:
                raise GatewayError('UPSTREAM_RATE_LIMITED', '기업 챗봇 호출 한도를 초과했습니다.', True)
            if not 200 <= response.status < 300:
                raise GatewayError('UPSTREAM_HTTP_ERROR', '기업 챗봇이 오류 상태를 반환했습니다.', response.status >= 500)
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise GatewayError('INVALID_UPSTREAM_RESPONSE', '압축되지 않은 JSON 응답이 필요합니다.')
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise GatewayError('UPSTREAM_RESPONSE_TOO_LARGE', '기업 챗봇 응답이 너무 큽니다.')
            try:
                response_body = json.loads(body)
                answer = read_pointer(response_body, config['answerPointer'])
                evidence = {}
                for field, setting in (('documents', 'retrievalPointer'), ('citations', 'citationsPointer')):
                    if config.get(setting):
                        evidence[field] = read_pointer(response_body, config[setting])
                evidence = Evidence.model_validate(evidence).model_dump()
            except (ValueError, TypeError, RecursionError):
                raise GatewayError('INVALID_UPSTREAM_RESPONSE', 'JSON 답변 경로를 확인해 주세요.') from None
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 100_000:
                raise GatewayError('INVALID_UPSTREAM_RESPONSE', '답변은 1~100,000자의 문자열이어야 합니다.')
            return ChatbotReply(answer, evidence) if config.get('retrievalPointer') or config.get('citationsPointer') else answer


async def execute_chatbot(config: dict, question: str, prompt: str | None,
                          secret: str | None, *, sender=None, apply_prompt=True,
                          llm_secret: str | None = None) -> GatewayResult:
    """No session sharing, no secret logging, and only explicitly safe bounded retries."""
    sender = sender or send_once
    start = monotonic()
    payload = deepcopy(config['requestTemplate'])
    write_pointer(payload, config['questionPointer'], question)
    if config['executionMode'] == 'PROMPT_BODY' and apply_prompt:
        if prompt is None:
            raise ApiError(400, 'VALIDATION_ERROR', '프롬프트 버전이 필요합니다.')
        write_pointer(payload, config['promptPointer'], prompt)
    headers = {**config['headers'], 'Accept-Encoding': 'identity'}
    auth = config['auth']
    if auth['type'] != 'NONE':
        if secret is None:
            raise ApiError(409, 'CONNECTION_SECRET_REQUIRED', '인증정보를 먼저 등록해 주세요.')
        headers['Authorization' if auth['type'] == 'BEARER' else auth['headerName']] = (
            f'Bearer {secret}' if auth['type'] == 'BEARER' else secret)
    # 평가 대상이 답변 생성에 쓰는 AI 키. 헤더 이름을 정했고 키가 등록돼 있을 때만 보냄
    if config.get('llmKeyHeaderName') and llm_secret:
        headers[config['llmKeyHeaderName']] = llm_secret
    hidden = [value for value in (secret, llm_secret) if value]
    retries = config['maxRetries'] if config['retrySafe'] else 0
    for attempt in range(1, retries + 2):
        try:
            # Includes DNS resolution, connect, response headers and streaming body.
            async with asyncio.timeout(config['timeoutMs'] / 1000):
                reply = await sender(config, deepcopy(payload), dict(headers))
            answer = reply.answer if isinstance(reply, ChatbotReply) else reply
            evidence = reply.evidence if isinstance(reply, ChatbotReply) else Evidence().model_dump()
            for value in hidden:
                answer = answer.replace(value, '[REDACTED]')
                evidence = redact_evidence(evidence, value)
            return GatewayResult(answer, int((monotonic() - start) * 1000), attempt, None, evidence)
        except TimeoutError:
            error = GatewayError('UPSTREAM_TIMEOUT', '기업 챗봇 응답 시간이 초과되었습니다.', True)
        except ApiError as exc:
            error = GatewayError(exc.code, exc.message)
        except GatewayError as exc:
            error = exc
        except aiohttp.ClientError:
            error = GatewayError('UPSTREAM_CONNECTION_FAILED', '기업 챗봇에 연결할 수 없습니다.', True)
        if attempt > retries or not error.retryable:
            return GatewayResult(None, int((monotonic() - start) * 1000), attempt,
                                 {'code': error.code, 'message': error.message})
        await asyncio.sleep(0.25 * attempt)


def redact_evidence(value, secret):
    """Mask credential echoes recursively without changing the structured response shape."""
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]')
    if isinstance(value, list):
        return [redact_evidence(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: redact_evidence(item, secret) for key, item in value.items()}
    return value
