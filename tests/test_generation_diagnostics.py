from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.main import app
from app.models.policy import GenerationRequest, QuestionHistory
from app.services.question_generation import get_question_generator
from test_policy_testsets import generated_question, setup


@pytest.mark.parametrize('failure,code', [
    ('schema', 'GENERATION_SCHEMA_INVALID'),
    ('category', 'GENERATION_CATEGORY_MISMATCH'),
    ('rule', 'GENERATION_EVIDENCE_INVALID'),
])
def test_invalid_semantic_draft_rolls_back_atomically(db_and_client, failure, code):
    factory, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    result = generated_question(question)
    if failure == 'schema':
        result.pop('question')
    elif failure == 'category':
        result['category'] = 'exception'
    else:
        result['ruleKeys'] = ['invented']
        result['expectedConditions'][0]['ruleKeys'] = ['invented']

    class Generator:
        def generate(self, **kwargs):
            return {'questions': [result]}

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


def test_short_batch_is_retried_and_saved_once(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    calls = 0

    class Generator:
        def generate(self, **kwargs):
            nonlocal calls
            calls += 1
            return {'questions': [generated_question(question, f' ({calls})')]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    response = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 2,
        'categoryCounts': {'normal': 2}},
        headers={**headers, 'Idempotency-Key': 'retry-short'})
    assert response.status_code == 200, response.text
    assert len(response.json()['data']['questions']) == 2 and calls == 2


def test_invented_rule_keys_are_repaired_not_fatal(db_and_client):
    """A draft whose question-level keys are invented but one condition is grounded is kept."""
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    result = generated_question(question)
    result['ruleKeys'] = ['invented']
    grounded = deepcopy(result['expectedConditions'][0])
    invented = {**deepcopy(grounded), 'description': '없는 규칙 조건', 'ruleKeys': ['invented']}
    grounded['ruleKeys'] = grounded['ruleKeys'] + ['also-invented']
    result['expectedConditions'] = [invented, grounded]

    class Generator:
        def generate(self, **kwargs):
            return {'questions': [result]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    response = client.post(path + '/questions/generate',
        json={'expectedRevision': 1, 'count': 1, 'categories': ['normal']},
        headers={**headers, 'Idempotency-Key': 'repair-keys'})
    assert response.status_code == 200, response.text
    saved = response.json()['data']['questions'][0]
    assert saved['ruleKeys'] == ['refund']
    assert [c['ruleKeys'] for c in saved['expectedConditions']] == [['refund']]


def test_large_requests_are_split_into_small_calls_and_extras_trimmed(db_and_client):
    """Twelve questions become calls of at most five; a model returning extras never fails the batch."""
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    sizes = []

    class Generator:
        def generate(self, *, count, hints=None, **kwargs):
            sizes.append(count)
            # Always over-produce: extras must be trimmed, not rejected.
            return {'questions': [generated_question(question, f' {len(sizes)}-{i}-{id(hints)}')
                                  for i in range(count + 2)]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    response = client.post(path + '/questions/generate',
        json={'expectedRevision': 1, 'count': 12, 'categories': ['normal']},
        headers={**headers, 'Idempotency-Key': 'split'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['questionCount'] == 12
    assert sorted(sizes) == [2, 5, 5]
