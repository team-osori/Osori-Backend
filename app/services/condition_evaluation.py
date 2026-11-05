"""Expectation validation and the policy-first scoring profile."""
from copy import deepcopy

from app.schemas.condition_evaluation import ConditionJudgement, ExpectedCondition

CRITERIA = {
    'version': 2,
    'definitions': {
        'policy': '정책의 필수 조건과 금지 행동을 준수하는가',
        'questionResolution': '질문 해결에 필요한 조건·절차·추가 질문을 안내하는가',
        'communicationQuality': '명확하고 친절하며 불필요하게 장황하지 않은가',
    },
    'scoringAnchors': {
        'questionResolution': ['필요 안내 없음', '주요 조건 누락', '일부 안내', '필요 조건·절차 안내', '조건·절차·추가 질문 적절'],
        'communicationQuality': ['이해 불가', '혼란·무례', '일부 불명확', '명확·친절', '명확·친절·불필요한 장황함 없음'],
    },
    'passThreshold': 3,
}


def prepare_questions(testset):
    """Normalize an immutable B snapshot without changing A's source records."""
    result = deepcopy(testset)
    policies = {policy['id']: policy['content'] for policy in result['policies']}
    for question in result['questions']:
        conditions = question.get('expectedConditions')
        if conditions is None:
            # Existing reviewed questions have one identified legacy policy expectation.
            conditions = [{'id': 'legacy-' + question['id'], 'criterion': 'policy',
                'requirement': question['expectedBehavior'], 'policyVersionId': question['policyVersionId'],
                'sourceQuote': question['evidenceQuote']}]
        if not 1 <= len(conditions) <= 100:
            raise ValueError('Expected conditions must be bounded and nonempty')
        parsed = [ExpectedCondition.model_validate(condition).model_dump() for condition in conditions]
        if len({condition['id'] for condition in parsed}) != len(parsed):
            raise ValueError('Duplicate condition IDs')
        for condition in parsed:
            if condition['sourceQuote'] not in policies.get(condition['policyVersionId'], ''):
                raise ValueError('Expected condition source does not match frozen policies')
        question['expectedConditions'] = parsed
    return result


def unavailable(question, reason, error=None):
    results = {key: {'question_id': question['id'], 'status': 'error' if error else 'not_evaluable',
        'verdict': None, 'reason': reason, 'evidence_refs': [], 'error': deepcopy(error)} for key in CRITERIA['definitions']}
    for key in ('questionResolution', 'communicationQuality'):
        results[key]['score'] = None
    results['conditionResults'] = [dict(results['policy'], conditionId=condition['id'])
                                  for condition in question['expectedConditions']]
    return results


def validate_judgement(output, question, catalog):
    """Require complete, source-backed condition decisions before accepting any grades."""
    output = ConditionJudgement.model_validate(output).model_dump()
    conditions = {condition['id']: condition for condition in question['expectedConditions']}
    decisions = output['conditionResults']
    if len(decisions) != len(conditions) or {decision['conditionId'] for decision in decisions} != set(conditions):
        raise ValueError('Missing, duplicate or foreign condition result')
    sources = {source['evidence_id']: source for source in catalog}
    for result in [output[key] for key in CRITERIA['definitions']] + decisions:
        if result['question_id'] != question['id'] or any(ref['evidence_id'] not in sources for ref in result['evidence_refs']):
            raise ValueError('Unknown question or evidence reference')
    for decision in decisions:
        if decision['status'] == 'judged':
            condition = conditions[decision['conditionId']]
            references = [sources[ref['evidence_id']] for ref in decision['evidence_refs']]
            if not any(ref['kind'] == 'policy' and ref['source_id'] == condition['policyVersionId'] for ref in references):
                raise ValueError('A condition verdict requires its policy source')
            if not any(ref['kind'] == 'answer' for ref in references):
                raise ValueError('A condition verdict requires the evaluated answer')
            quote = (decision.get('issue') or {}).get('answerQuote')
            answer = next(source['text'] for source in catalog if source['kind'] == 'answer')
            if quote and quote not in answer:
                raise ValueError('Issue quote must be present verbatim in the evaluated answer')
            hypothesis = (decision.get('issue') or {}).get('hypothesis')
            if hypothesis and any(ref['evidence_id'] not in sources for ref in hypothesis['evidenceRefs']):
                raise ValueError('A cause hypothesis must reference collected evidence')
    policy_decisions = [decision for decision in decisions if conditions[decision['conditionId']]['criterion'] == 'policy']
    if policy_decisions:
        expected = 'fail' if any(d['verdict'] == 'fail' for d in policy_decisions) else (
            'pass' if all(d['verdict'] == 'pass' for d in policy_decisions) else None)
        if output['policy']['verdict'] != expected:
            raise ValueError('Policy summary contradicts its condition decisions')
    return output
