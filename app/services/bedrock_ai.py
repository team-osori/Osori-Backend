"""Bedrock implementations of the existing question-generation and judgment contracts."""
import hashlib
import os

from pydantic import ValidationError

from app.core.errors import ApiError
from app.schemas.condition_evaluation import ConditionJudgement
from app.schemas.evaluation import Judgement
from app.services import bedrock_runtime as runtime
from app.services.evaluation_judge import JudgeError
from app.services.evaluation_judge_contract import INSTRUCTIONS as JUDGE_INSTRUCTIONS, judgment_schema
from app.services.question_generation import (INSTRUCTIONS as GENERATION_INSTRUCTIONS,
    generation_payload, generation_schema, parse_generated_questions, select_generated_questions)

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

    def generate(self, *, policies, count, categories=None, category_counts=None, hints=None):
        selected = [k for k, v in category_counts.items() if v] if category_counts is not None else categories or ['normal', 'exception', 'boundary'][:min(count, 3)]
        payload = generation_payload(policies, count, selected, category_counts, hints)
        try:
            output = runtime.converse_json(model=self.model, region=self.region,
                instructions=GENERATION_INSTRUCTIONS, payload=payload,
                schema=generation_schema(), name='submit_questions', max_tokens=min(32000, 2000 + count * 2000),
                read_timeout=120)
            questions, dropped = parse_generated_questions(output)
            if not questions and dropped:
                raise ValueError('No well-formed question')
            return select_generated_questions(questions, count, selected, category_counts)
        except runtime.BedrockError as exc:
            code = 'GENERATION_SCHEMA_INVALID' if exc.code == 'INVALID_OUTPUT' else 'GENERATION_' + exc.code
            status = 400 if exc.code == 'INPUT_TOO_LARGE' else 504 if exc.code == 'TIMEOUT' else (
                502 if exc.code in ('INCOMPLETE', 'REFUSED', 'INVALID_OUTPUT', 'RESPONSE_TOO_LARGE') else 503)
            raise ApiError(status, code, MESSAGES[exc.code]) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ApiError(502, 'GENERATION_SCHEMA_INVALID', MESSAGES['INVALID_OUTPUT']) from None


JUDGE_ATTEMPTS = 3
# The last attempt uses a stronger model when the configured one keeps returning incomplete output.
JUDGE_FALLBACK_MODEL = 'global.anthropic.claude-sonnet-4-6'
JUDGE_SECTIONS = ('policy', 'questionResolution', 'communicationQuality', 'conditionResults')
JUDGE_REMINDER = ('\n도구 입력에는 policy, questionResolution, communicationQuality, conditionResults 네 필드를 모두 넣는다. '
    'conditionResults에는 question.expectedConditions의 모든 조건 id마다 결과를 하나씩 넣는다. '
    '모든 결과 객체에 question_id, status, verdict, reason, evidence_refs, error(없으면 null)를 넣고, '
    'questionResolution과 communicationQuality에는 score(판정했으면 0~4, 판정 못하면 null)를, '
    'conditionResults의 각 결과에는 conditionId와 issue(fail일 때만, 아니면 null)를 넣는다. '
    'fail인 조건의 issue에는 hypothesis를 반드시 넣는다. 정책 원문은 검색됐는데 답변이 조건·확인 절차를 빠뜨렸거나 '
    '주어진 정보만으로 단정했다면 area는 PROMPT로 한다.')


def normalize_judgement(output, question_id):
    """Fill fields the model commonly omits but whose value is implied; never invent a verdict."""
    if not isinstance(output, dict):
        return output
    results = [output.get(key) for key in JUDGE_SECTIONS[:3]]
    conditions = output.get('conditionResults')
    if isinstance(conditions, list):
        results += conditions
    for result in results:
        if not isinstance(result, dict):
            continue
        result.setdefault('question_id', question_id)
        result.setdefault('error', None)
        refs = result.get('evidence_refs')
        if refs is None:
            result['evidence_refs'] = []
        elif isinstance(refs, list):
            seen, unique = set(), []
            for ref in refs:
                key = ref.get('evidence_id') if isinstance(ref, dict) else None
                if key not in seen:
                    seen.add(key)
                    unique.append(ref)
            result['evidence_refs'] = unique
        if result.get('status') != 'judged' and 'score' in result:
            result['score'] = None
    for result in conditions if isinstance(conditions, list) else []:
        if isinstance(result, dict) and (result.get('status') != 'judged' or result.get('verdict') != 'fail'):
            result['issue'] = None
        elif isinstance(result, dict):
            result.setdefault('issue', None)
    return output


def attach_required_refs(output, question, catalog):
    """Cite what every condition verdict is judged against by definition: its policy and the answer.

    The model's verdicts are kept as-is; only citations the server can determine are added,
    references to evidence IDs that do not exist are dropped, and the policy summary follows
    its policy conditions (the rule validate_judgement enforces).
    """
    if not isinstance(output, dict) or not catalog:
        return output
    known = {source['evidence_id']: source for source in catalog}
    answer = next((s for s in catalog if s['kind'] == 'answer'), None)

    def policy_for(version_id):
        return next((s for s in catalog if s['kind'] == 'policy' and s['source_id'] == version_id), None)

    def ensure(result, required):
        refs = [ref for ref in result.get('evidence_refs') or []
                if isinstance(ref, dict) and ref.get('evidence_id') in known]
        have = {ref['evidence_id'] for ref in refs}
        added = [{'evidence_id': source['evidence_id'], 'explanation': text} for source, text in required
                 if source is not None and source['evidence_id'] not in have]
        result['evidence_refs'] = (added + refs)[:20]

    conditions = {c.get('id'): c for c in question.get('expectedConditions') or []}
    decisions = output.get('conditionResults') if isinstance(output.get('conditionResults'), list) else []
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get('status') != 'judged':
            continue
        condition = conditions.get(decision.get('conditionId')) or {}
        ensure(decision, [(policy_for(condition.get('policyVersionId')), '이 조건의 근거 정책 원문'),
                          (answer, '판정 대상 챗봇 답변')])
        issue = decision.get('issue')
        if isinstance(issue, dict) and answer and issue.get('answerQuote') and issue['answerQuote'] not in answer['text']:
            issue['answerQuote'] = None
            if not issue.get('missingGuidance'):
                issue['missingGuidance'] = [condition.get('requirement') or decision.get('reason') or '조건 미충족']
    for key in JUDGE_SECTIONS[:3]:
        result = output.get(key)
        if isinstance(result, dict) and result.get('status') == 'judged':
            ensure(result, [(answer, '판정 대상 챗봇 답변')])
    policy = output.get('policy')
    verdicts = [d.get('verdict') for d in decisions if isinstance(d, dict)
                and (conditions.get(d.get('conditionId')) or {}).get('criterion') == 'policy']
    if isinstance(policy, dict) and verdicts:
        expected = 'fail' if 'fail' in verdicts else 'pass' if all(v == 'pass' for v in verdicts) else None
        if expected is not None and policy.get('status') == 'judged':
            policy['verdict'] = expected
    return output


def judgement_problems(error):
    """Turn a validation failure into short, field-level feedback for the retry."""
    if isinstance(error, ValidationError):
        return [f"{'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}" for item in error.errors()[:15]]
    return [str(error)[:300] or '형식 오류']


class BedrockEvaluationJudge:
    """Collect structured verdicts without blocking the asynchronous evaluation worker."""
    # Up to JUDGE_ATTEMPTS model calls per answer.
    timeout_seconds = 330

    def __init__(self, model, region):
        self.model, self.region = model, region

    def descriptor(self):
        return {'adapter': 'bedrock-converse-tool-v1', 'provider': 'bedrock', 'model': self.model,
                'region': self.region, 'instructionHash': hashlib.sha256(JUDGE_INSTRUCTIONS.encode()).hexdigest(),
                'contractVersion': 2, 'outputMode': 'tool_use', 'temperature': 0, 'maxOutputTokens': 24000,
                'timeoutSeconds': self.timeout_seconds}

    async def judge(self, **kwargs):
        version = kwargs['criteria']['version']
        model = ConditionJudgement if version == 2 else Judgement
        question_id = kwargs['question'].get('id')
        base = {**kwargs, 'policies': [{key: value for key, value in policy.items() if key != 'content'}
                                     for policy in kwargs['policies']]}
        problems = []
        for attempt in range(JUDGE_ATTEMPTS):
            # A retry tells the model exactly what was missing instead of failing the whole question.
            payload = {**base, 'previousAttemptProblems': problems} if problems else base
            try:
                last = attempt == JUDGE_ATTEMPTS - 1
                fallback = os.getenv('EVALUATION_JUDGE_FALLBACK_MODEL', JUDGE_FALLBACK_MODEL).strip()
                output = await runtime.converse_json_async(model=fallback if last and fallback else self.model,
                    region=self.region, instructions=JUDGE_INSTRUCTIONS + (JUDGE_REMINDER if version == 2 else ''), payload=payload,
                    schema=judgment_schema(version), name='submit_evaluation',
                    max_tokens=min(24000, 3000 + len(kwargs['question'].get('expectedConditions', [])) * 900),
                    read_timeout=110)
                if version == 2:
                    output = attach_required_refs(normalize_judgement(output, question_id), kwargs['question'],
                                                  kwargs.get('evidence_catalog') or [])
                result = model.model_validate(output)
                if version == 2 and kwargs.get('prompt_context') is not None:
                    # Same identity checks the worker applies, so a mismatch is retried here.
                    from app.services.condition_evaluation import validate_judgement
                    validate_judgement(result.model_dump(), kwargs['question'], kwargs['evidence_catalog'],
                                       kwargs['prompt_context'])
                return result
            except runtime.BedrockError as exc:
                if exc.code not in ('INVALID_OUTPUT', 'INCOMPLETE') or attempt == JUDGE_ATTEMPTS - 1:
                    code = 'INVALID_JUDGE_OUTPUT' if exc.code == 'INVALID_OUTPUT' else 'JUDGE_' + exc.code
                    raise JudgeError(code, MESSAGES[exc.code]) from None
                problems = ['도구 입력이 완성되지 않았거나 형식이 맞지 않았습니다.']
            except (ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
                if attempt == JUDGE_ATTEMPTS - 1:
                    raise JudgeError('INVALID_JUDGE_OUTPUT', MESSAGES['INVALID_OUTPUT']) from None
                problems = judgement_problems(exc)
