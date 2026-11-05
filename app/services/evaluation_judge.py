from typing import Protocol
import os

from app.schemas.evaluation import Judgement
from app.schemas.condition_evaluation import ConditionJudgement

CRITERIA = {
    'version': 1,
    'definitions': {
        'retrieval': '질문에 필요한 정책 근거가 검색 문서에 포함되는가',
        'grounding': '답변이 반환된 검색 문서와 인용 근거에 충실한가',
        'response': '기대 행동을 충족하는 명확한 답변인가',
        'policy': '적용 정책 및 금지 행동을 준수하는가',
    },
}


class EvaluationJudge(Protocol):
    """Provider boundary: enforce timeouts and the submitted profile's output contract."""
    def descriptor(self) -> dict:
        """Return non-secret adapter/model identity persisted at submission."""
        ...

    async def judge(self, *, question: dict, answer: str, evidence: dict, policies: list, criteria: dict, evidence_catalog: list) -> Judgement | ConditionJudgement | dict:
        """Judge one response using frozen inputs; never include credentials in output or logs."""
        ...


class UnconfiguredJudge:
    """No network access, invented grades or keyword-based substitute for a real judge."""
    def descriptor(self):
        """Leave provider and model blank until the team chooses a real adapter."""
        return {'adapter': 'unconfigured', 'provider': None, 'model': None}

    async def judge(self, **kwargs):
        """Keep every criterion ungraded while preserving the completed company response."""
        if kwargs['criteria']['version'] == 2:
            from app.services.condition_evaluation import unavailable
            return unavailable(kwargs['question'], '평가 모델 미연결')
        return Judgement.model_validate({key: {'question_id': kwargs['question']['id'],
            'status': 'not_evaluable', 'verdict': None, 'reason': '평가 모델 미연결', 'evidence_refs': [], 'error': None}
            for key in CRITERIA['definitions']})


def get_evaluation_judge() -> EvaluationJudge:
    """Require explicit opt-in; allow the team's question-generation key to be reused."""
    if os.getenv('EVALUATION_JUDGE_PROVIDER', '').strip().lower() == 'openai':
        key = (os.getenv('EVALUATION_JUDGE_API_KEY', '').strip()
               or os.getenv('QUESTION_GENERATION_API_KEY', '').strip()
               or os.getenv('OPENAI_API_KEY', '').strip())
        if key:
            from app.services.openai_evaluation_judge import OpenAIEvaluationJudge
            from app.services.question_generation import DEFAULT_MODEL
            model = (os.getenv('EVALUATION_JUDGE_MODEL', '').strip()
                     or os.getenv('QUESTION_GENERATION_MODEL', '').strip() or DEFAULT_MODEL)
            return OpenAIEvaluationJudge(key, model)
    return UnconfiguredJudge()
