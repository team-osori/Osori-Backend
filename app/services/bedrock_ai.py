"""Bedrock implementations of the existing question-generation and judgment contracts."""
import hashlib

from app.core.errors import ApiError
from app.schemas.condition_evaluation import ConditionJudgement
from app.schemas.evaluation import Judgement
from app.schemas.policy import GeneratedQuestions
from app.services import bedrock_runtime as runtime
from app.services.evaluation_judge import JudgeError
from app.services.evaluation_judge_contract import INSTRUCTIONS as JUDGE_INSTRUCTIONS, judgment_schema
from app.services.question_generation import (INSTRUCTIONS as GENERATION_INSTRUCTIONS,
    generation_schema, validate_generated_questions)

MESSAGES = {
    'AUTH_FAILED': '서버의 Bedrock 호출 권한과 IAM 역할을 확인하세요.',
    'RATE_LIMITED': 'Bedrock 호출 한도 또는 사용 할당량을 확인하세요.',
    'TIMEOUT': 'Bedrock 응답 시간이 초과되었습니다.',
    'INVALID_REQUEST': 'Bedrock 리전·모델 ID·도구 호출 지원 및 요청 설정을 확인하세요.',
    'UNAVAILABLE': 'Bedrock 서비스에 연결할 수 없습니다.',
    'INCOMPLETE': '모델 출력이 완료되지 않았습니다. 입력 범위를 줄여 주세요.',
    'REFUSED': '모델이 해당 자료의 처리를 거절했습니다.',
    'INVALID_OUTPUT': '유효한 구조화 결과를 받지 못했습니다.',
    'INPUT_TOO_LARGE': '입력은 최대 512KiB입니다. 정책·답변·검색 자료의 범위를 줄여 주세요.',
    'RESPONSE_TOO_LARGE': '모델 응답이 허용 크기를 초과했습니다.',
    'BUSY': '이전 Bedrock 판정 호출이 아직 종료되지 않았습니다. 종료 후 새 평가를 요청하세요.',
}


class BedrockQuestionGenerator:
    """Generate drafts only; policy validation, server IDs and human review remain unchanged."""
    def __init__(self, model, region):
        self.model, self.region = model, region

    def generate(self, *, policies, count, categories=None, category_counts=None):
        selected = [k for k, v in category_counts.items() if v] if category_counts is not None else categories or ['normal', 'exception', 'boundary'][:min(count, 3)]
        payload = {'policies': policies, 'count': count, 'categories': selected}
        if category_counts is not None:
            payload['categoryCounts'] = category_counts
        try:
            output = runtime.converse_json(model=self.model, region=self.region,
                instructions=GENERATION_INSTRUCTIONS, payload=payload,
                schema=generation_schema(), name='submit_questions', max_tokens=min(32000, 1000 + count * 1400),
                read_timeout=55)
            generated = GeneratedQuestions.model_validate(output)
            return validate_generated_questions(generated, count, selected, category_counts)
        except runtime.BedrockError as exc:
            code = 'GENERATION_SCHEMA_INVALID' if exc.code == 'INVALID_OUTPUT' else 'GENERATION_' + exc.code
            status = 400 if exc.code == 'INPUT_TOO_LARGE' else 504 if exc.code == 'TIMEOUT' else (
                502 if exc.code in ('INCOMPLETE', 'REFUSED', 'INVALID_OUTPUT', 'RESPONSE_TOO_LARGE') else 503)
            raise ApiError(status, code, MESSAGES[exc.code]) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ApiError(502, 'GENERATION_SCHEMA_INVALID', MESSAGES['INVALID_OUTPUT']) from None


class BedrockEvaluationJudge:
    """Collect structured verdicts without blocking the asynchronous evaluation worker."""
    timeout_seconds = 120

    def __init__(self, model, region):
        self.model, self.region = model, region

    def descriptor(self):
        return {'adapter': 'bedrock-converse-tool-v1', 'provider': 'bedrock', 'model': self.model,
                'region': self.region, 'instructionHash': hashlib.sha256(JUDGE_INSTRUCTIONS.encode()).hexdigest(),
                'contractVersion': 2, 'outputMode': 'tool_use', 'temperature': 0, 'maxOutputTokens': 24000,
                'timeoutSeconds': self.timeout_seconds}

    async def judge(self, **kwargs):
        payload = {**kwargs, 'policies': [{key: value for key, value in policy.items() if key != 'content'}
                                        for policy in kwargs['policies']]}
        try:
            output = await runtime.converse_json_async(model=self.model, region=self.region,
                instructions=JUDGE_INSTRUCTIONS, payload=payload,
                schema=judgment_schema(kwargs['criteria']['version']), name='submit_evaluation',
                max_tokens=min(24000, 2000 + len(kwargs['question'].get('expectedConditions', [])) * 750),
                read_timeout=110)
            model = ConditionJudgement if kwargs['criteria']['version'] == 2 else Judgement
            return model.model_validate(output)
        except runtime.BedrockError as exc:
            code = 'INVALID_JUDGE_OUTPUT' if exc.code == 'INVALID_OUTPUT' else 'JUDGE_' + exc.code
            raise JudgeError(code, MESSAGES[exc.code]) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise JudgeError('INVALID_JUDGE_OUTPUT', MESSAGES['INVALID_OUTPUT']) from None
