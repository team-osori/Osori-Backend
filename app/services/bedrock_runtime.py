"""Bounded Converse transport using the AWS SDK credential chain, including EC2 IAM roles."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
import json
import os
from threading import BoundedSemaphore

import boto3
from botocore.config import Config
from botocore.exceptions import (BotoCoreError, ClientError, ConnectTimeoutError,
    CredentialRetrievalError, NoCredentialsError, ParamValidationError,
    PartialCredentialsError, ReadTimeoutError)

DEFAULT_REGION = 'ap-northeast-3'
DEFAULT_MODEL = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'
MAX_INPUT_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class BedrockError(Exception):
    """Never retain raw AWS exception bodies or credential details in application errors."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _BoundedJudgeCalls:
    """Keep one unfinished SDK call per process, including calls whose waiter was cancelled."""
    def __init__(self):
        """Create a single-call budget and an executor isolated from asyncio's default pool."""
        self._slots = BoundedSemaphore(1)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bedrock-judge')

    async def run(self, call, **kwargs):
        """Keep capacity owned by the underlying concurrent future, not by its async waiter."""
        if not self._slots.acquire(blocking=False):
            raise BedrockError('BUSY')
        try:
            future = self._executor.submit(copy_context().run, call, **kwargs)
        except RuntimeError:
            self._slots.release()
            raise BedrockError('UNAVAILABLE') from None
        except BaseException:
            self._slots.release()
            raise
        # Cancellation of an asyncio waiter cannot stop a running boto3 call.
        # Release only when the concurrent future really finishes (or cancels before starting).
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.wrap_future(future)


_judge_calls = _BoundedJudgeCalls()


async def converse_json_async(**kwargs):
    """Isolate judgment SDK calls from asyncio's default executor without an unbounded queue."""
    return await _judge_calls.run(converse_json, **kwargs)


def configured_region():
    """Prefer explicit service settings over the competition server's default region."""
    return os.getenv('AWS_REGION', '').strip() or os.getenv('AWS_DEFAULT_REGION', '').strip() or DEFAULT_REGION


@lru_cache(maxsize=4)
def client_for(region, read_timeout):
    """Reuse thread-safe clients whose IAM role credentials are refreshed by the SDK."""
    return boto3.Session().client('bedrock-runtime', region_name=region, config=Config(
        connect_timeout=5, read_timeout=read_timeout, retries={'total_max_attempts': 1, 'mode': 'standard'},
        proxies={}, ignore_configured_endpoint_urls=True))


def converse_json(*, model, region, instructions, payload, schema, name, max_tokens, read_timeout):
    """Collect one structured tool input; this function never executes any requested tool."""
    source = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(source.encode()) > MAX_INPUT_BYTES:
        raise BedrockError('INPUT_TOO_LARGE')
    request = {'modelId': model,
        'system': [{'text': instructions + '\n결과는 지정된 도구 입력으로 한 번만 제출한다.'}],
        'messages': [{'role': 'user', 'content': [{'text': source}]}],
        'inferenceConfig': {'maxTokens': max_tokens, 'temperature': 0},
        'toolConfig': {'tools': [{'toolSpec': {'name': name, 'description': '검증할 구조화 결과를 제출한다.',
                                              'inputSchema': {'json': schema}}}],
                       'toolChoice': {'tool': {'name': name}}}}
    try:
        response = client_for(region, read_timeout).converse(**request)
    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
        raise BedrockError('AUTH_FAILED') from None
    except (ReadTimeoutError, ConnectTimeoutError, TimeoutError):
        raise BedrockError('TIMEOUT') from None
    except ParamValidationError:
        raise BedrockError('INVALID_REQUEST') from None
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        category = {'AccessDeniedException': 'AUTH_FAILED', 'UnrecognizedClientException': 'AUTH_FAILED',
            'ExpiredTokenException': 'AUTH_FAILED', 'InvalidSignatureException': 'AUTH_FAILED',
            'ThrottlingException': 'RATE_LIMITED', 'ServiceQuotaExceededException': 'RATE_LIMITED',
            'ModelTimeoutException': 'TIMEOUT', 'RequestTimeout': 'TIMEOUT',
            'ValidationException': 'INVALID_REQUEST', 'ResourceNotFoundException': 'INVALID_REQUEST'}
        raise BedrockError(category.get(code, 'UNAVAILABLE')) from None
    except BotoCoreError:
        raise BedrockError('UNAVAILABLE') from None
    try:
        if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise BedrockError('RESPONSE_TOO_LARGE')
        reason = response.get('stopReason')
        if reason in ('guardrail_intervened', 'content_filtered'):
            raise BedrockError('REFUSED')
        if reason in ('max_tokens', 'model_context_window_exceeded'):
            raise BedrockError('INCOMPLETE')
        message = response['output']['message']
        tools = [block['toolUse'] for block in message['content'] if 'toolUse' in block]
        if (reason != 'tool_use' or message.get('role') != 'assistant' or len(tools) != 1
                or tools[0]['name'] != name or not isinstance(tools[0]['input'], dict)):
            raise BedrockError('INVALID_OUTPUT')
        return tools[0]['input']
    except BedrockError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BedrockError('INVALID_OUTPUT') from None
