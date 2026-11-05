from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.main import app
from app.models.policy import GenerationRequest, QuestionHistory
from app.services.question_generation import get_question_generator
from test_policy_testsets import setup


@pytest.mark.parametrize('failure,code', [
    ('schema', 'GENERATION_SCHEMA_INVALID'),
    ('count', 'GENERATION_COUNT_MISMATCH'),
    ('category', 'GENERATION_CATEGORY_MISMATCH'),
    ('evidence', 'GENERATION_EVIDENCE_INVALID'),
    ('rule', 'GENERATION_EVIDENCE_INVALID'),
    ('policy', 'GENERATION_EVIDENCE_INVALID'),
    ('condition', 'GENERATION_EVIDENCE_INVALID'),
    ('condition_id', 'GENERATION_CONDITIONS_INVALID'),
])
def test_failure_codes_and_atomic_rollback(db_and_client, failure, code):
    factory, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    result = deepcopy(question)
    if failure == 'schema':
        result.pop('question')
    elif failure == 'category':
        result['category'] = 'exception'
    elif failure == 'evidence':
        result['evidenceQuote'] = '예약 취소는 출발 48시간 전까지 가능합니다.'
    elif failure == 'rule':
        result['ruleKeys'] = ['invented']
    elif failure == 'policy':
        result['policyVersionId'] = 'invented'
    elif failure.startswith('condition'):
        result['expectedConditions'] = [{'description': '취소 기한 안내', 'kind': 'required',
            'policyVersionId': question['policyVersionId'], 'ruleKeys': ['refund'],
            'evidenceQuote': '허위 근거' if failure == 'condition' else question['evidenceQuote']}]
        if failure == 'condition_id':
            result['expectedConditions'][0]['id'] = 'invented'

    class Generator:
        def generate(self, **kwargs):
            return {'questions': [result, result] if failure == 'count' else [result]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    response = client.post(path + '/questions/generate',
        json={'expectedRevision': 1, 'count': 1, 'categories': ['normal']},
        headers={**headers, 'Idempotency-Key': 'diagnostic'})
    assert response.status_code == 502, response.text
    assert response.json()['error']['code'] == code
    version = client.get(path, headers=headers).json()['data']
    assert version['revision'] == 1 and version['questions'] == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionHistory)) == 0
        assert db.scalar(select(func.count()).select_from(GenerationRequest)) == 0


def test_generated_whitespace_is_restored_to_verbatim_source(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    original = question['evidenceQuote']
    question['evidenceQuote'] = original.replace(' ', '\n\t')
    question['expectedConditions'] = [{'description': '취소 기한 안내', 'kind': 'required',
        'policyVersionId': question['policyVersionId'], 'ruleKeys': ['refund'],
        'evidenceQuote': question['evidenceQuote']}]
    # Manual evidence remains strict.
    assert client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers).status_code == 400

    class Generator:
        def generate(self, **kwargs):
            return {'questions': [question]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    response = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 1},
        headers={**headers, 'Idempotency-Key': 'whitespace'})
    assert response.status_code == 200, response.text
    saved = response.json()['data']['questions'][0]
    assert saved['evidenceQuote'] == original
    assert saved['expectedConditions'][0]['evidenceQuote'] == original
    assert saved['reviewStatus'] == 'pending'
