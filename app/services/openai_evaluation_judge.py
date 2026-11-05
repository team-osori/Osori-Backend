"""Bounded Responses API adapter for the frozen evaluation contract."""
import hashlib
import json

import aiohttp

from app.schemas.condition_evaluation import ConditionJudgement
from app.schemas.evaluation import Judgement
from app.services.evaluation_judge import JudgeError
from app.services.evaluation_judge_contract import INSTRUCTIONS, judgment_schema

MAX_INPUT_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_TOKENS = 24000


class OpenAIEvaluationJudge:
    def __init__(self, api_key, model):
        self._api_key = api_key
        self.model = model

    def descriptor(self):
        return {'adapter': 'openai-responses-v1', 'provider': 'openai', 'model': self.model,
                'instructionHash': hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
                'contractVersion': 2, 'maxOutputTokens': MAX_OUTPUT_TOKENS}

    async def _request(self, body):
        timeout = aiohttp.ClientTimeout(total=25, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post('https://api.openai.com/v1/responses', json=body,
                    headers={'Authorization': 'Bearer ' + self._api_key}, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise JudgeError('JUDGE_AUTH_FAILED', '판정 API 키 또는 모델 접근 권한을 확인하세요.')
                if response.status == 429:
                    raise JudgeError('JUDGE_RATE_LIMITED', '판정 API 한도 또는 크레딧을 확인하세요.')
                if response.status != 200:
                    raise JudgeError('JUDGE_UNAVAILABLE', '판정 서비스를 사용할 수 없습니다.')
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise JudgeError('JUDGE_RESPONSE_TOO_LARGE', '판정 응답이 허용 크기를 초과했습니다.')
                if self._api_key.encode() in data:
                    raise JudgeError('INVALID_JUDGE_OUTPUT', '판정 응답에 민감정보가 포함되어 저장하지 않았습니다.')
                return json.loads(data)

    async def judge(self, **kwargs):
        # Full policy text already lives in the server-owned evidence catalog.
        payload = {**kwargs, 'policies': [
            {key: value for key, value in policy.items() if key != 'content'}
            if isinstance(policy, dict) else policy for policy in kwargs.get('policies', [])]}
        source = json.dumps(payload, ensure_ascii=False)
        if len(source.encode()) > MAX_INPUT_BYTES:
            raise JudgeError('JUDGE_INPUT_TOO_LARGE', '판정할 정책·답변·검색 자료의 범위를 줄여 주세요.')
        count = len(kwargs['question'].get('expectedConditions', []))
        body = {'model': self.model, 'store': False, 'instructions': INSTRUCTIONS,
                'input': [{'role': 'user', 'content': source}],
                'max_output_tokens': min(MAX_OUTPUT_TOKENS, 2000 + count * 750),
                'text': {'format': {'type': 'json_schema', 'name': 'evaluation_judgment',
                                   'strict': True, 'schema': judgment_schema(kwargs['criteria']['version'])}}}
        try:
            result = await self._request(body)
            if result.get('status') != 'completed':
                raise JudgeError('JUDGE_INCOMPLETE', '판정 출력이 완료되지 않았습니다.')
            parts = [part for item in result.get('output', []) if item.get('type') == 'message'
                     for part in item.get('content', [])]
            if any(part.get('type') == 'refusal' for part in parts):
                raise JudgeError('JUDGE_REFUSED', '모델이 해당 자료의 판정을 거절했습니다.')
            texts = [part['text'] for part in parts if part.get('type') == 'output_text']
            if len(texts) != 1:
                raise ValueError('Missing structured judgment')
            # Also covers injected test transports, which bypass the HTTP byte reader.
            if self._api_key in texts[0]:
                raise JudgeError('INVALID_JUDGE_OUTPUT', '판정 응답에 민감정보가 포함되어 저장하지 않았습니다.')
            model = ConditionJudgement if kwargs['criteria']['version'] == 2 else Judgement
            return model.model_validate_json(texts[0])
        except JudgeError:
            raise
        except TimeoutError:
            raise JudgeError('JUDGE_TIMEOUT', '판정 모델 응답 시간이 초과되었습니다.') from None
        except aiohttp.ClientError:
            raise JudgeError('JUDGE_UNAVAILABLE', '판정 서비스에 연결할 수 없습니다.') from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise JudgeError('INVALID_JUDGE_OUTPUT', '유효한 구조화 판정을 받지 못했습니다.') from None
