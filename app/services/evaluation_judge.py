from typing import Protocol

from app.schemas.evaluation import Judgement

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
    """Provider boundary: implementations must enforce timeout and return all four criteria."""
    def descriptor(self) -> dict:
        """Return non-secret adapter/model identity persisted at submission."""
        ...

    async def judge(self, *, question: dict, answer: str, evidence: dict, policies: list, criteria: dict, evidence_catalog: list) -> Judgement:
        """Judge one response using frozen inputs; never include credentials in output or logs."""
        ...


class UnconfiguredJudge:
    """No network access, invented grades or keyword-based substitute for a real judge."""
    def descriptor(self):
        """Leave provider and model blank until the team chooses a real adapter."""
        return {'adapter': 'unconfigured', 'provider': None, 'model': None}

    async def judge(self, **kwargs):
        """Keep every criterion ungraded while preserving the completed company response."""
        return Judgement.model_validate({key: {'question_id': kwargs['question']['id'],
            'status': 'not_evaluable', 'verdict': None, 'reason': '평가 모델 미연결', 'evidence_refs': [], 'error': None}
            for key in CRITERIA['definitions']})


def get_evaluation_judge() -> EvaluationJudge:
    """Connect a selected provider here; environment placeholders alone do not enable it."""
    return UnconfiguredJudge()
