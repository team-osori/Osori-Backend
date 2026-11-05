from copy import deepcopy

import pytest

from app.main import app
from app.services.question_generation import get_question_generator


def account(client, login='owner'):
    """Create isolated API credentials without touching developer data."""
    body = {'loginId': login, 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json={**body, 'name': 'Test'}).status_code == 201
    tokens = client.post('/api/auth/login', json=body).json()['data']
    return {'Authorization': 'Bearer ' + tokens['accessToken']}


def setup(client, login='owner'):
    """Create original policy evidence and an empty human-review draft."""
    headers = account(client, login)
    project = client.post('/api/projects', json={'name': 'Test'}, headers=headers).json()['data']['id']
    base = f'/api/projects/{project}'
    policy = {'name': '환불 규정', 'content': '  예약 취소는 출발 24시간 전까지 가능합니다.\n임의 보상은 금지합니다.  ',
              'rules': [{'key': 'refund', 'description': '취소 기한을 안내한다.', 'sourceQuote': '예약 취소는 출발 24시간 전까지 가능합니다.'}]}
    response = client.post(base + '/policies', json=policy, headers=headers)
    assert response.status_code == 201, response.text
    source = response.json()['data']
    response = client.post(base + '/testsets', json={'name': '환불 검증', 'policyVersionIds': [source['version']['id']]}, headers=headers)
    assert response.status_code == 201, response.text
    data = response.json()['data']
    version_path = response.headers['location']
    question = {'question': '출발 30시간 전에 취소할 수 있나요?', 'expectedBehavior': '취소할 수 있다고 안내한다.',
                'forbiddenBehaviors': ['임의 보상을 약속한다.'], 'policyVersionId': source['version']['id'],
                'ruleKeys': ['refund'], 'evidenceQuote': '예약 취소는 출발 24시간 전까지 가능합니다.'}
    return headers, base, policy, source, data, version_path, question


def generated_question(question, suffix=''):
    return {'question': question['question'] + suffix, 'expectedBehavior': question['expectedBehavior'],
        'modelAnswer': question['expectedBehavior'], 'forbiddenBehaviors': question['forbiddenBehaviors'],
        'ruleKeys': question['ruleKeys'], 'category': question.get('category', 'normal'),
        'expectedConditions': [{'description': question['expectedBehavior'], 'kind': 'required', 'category': 'policy',
                                'ruleKeys': question['ruleKeys']}]}


def test_human_review_confirmation_and_immutable_evidence(db_and_client):
    """Confirmation freezes reviewed inputs even after a source edit or logical deletion."""
    _, client = db_and_client
    headers, base, policy, source, data, path, question = setup(client)
    assert client.get(path + '/evaluation-snapshot', headers=headers).status_code == 409
    assert client.post(path + '/confirm', json={'expectedRevision': 1}, headers=headers).status_code == 409
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
    response = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    assert response.status_code == 201, response.text
    version = response.json()['data']
    question_path = response.headers['location']
    assert version['revision'] == 2 and version['questions'][0]['reviewStatus'] == 'pending'
    assert client.post(path + '/confirm', json={'expectedRevision': 2}, headers=headers).status_code == 409
    version = client.post(question_path + '/review', json={'expectedRevision': 2, 'status': 'approved'}, headers=headers).json()['data']
    version = client.patch(question_path, json={**question, 'question': '취소 기한을 알려주세요.', 'expectedRevision': version['revision']}, headers=headers).json()['data']
    assert version['questions'][0]['reviewStatus'] == 'pending'
    assert version['questions'][0]['reviewedBy'] is None
    version = client.post(question_path + '/review', json={'expectedRevision': version['revision'], 'status': 'approved'}, headers=headers).json()['data']
    confirmed = client.post(path + '/confirm', json={'expectedRevision': version['revision']}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    snapshot = client.get(path + '/evaluation-snapshot', headers=headers).json()['data']
    assert snapshot['policies'][0]['content'] == policy['content']
    assert snapshot['questions'][0]['reviewStatus'] == 'approved'
    assert client.patch(question_path, json={**question, 'expectedRevision': confirmed.json()['data']['revision']}, headers=headers).status_code == 409
    source_path = base + '/policies/' + source['policy']['id']
    new_policy = client.post(source_path + '/versions', json={**policy, 'name': 'invalid', 'expectedRevision': 1}, headers=headers)
    assert new_policy.status_code == 400
    body = {k: v for k, v in policy.items() if k != 'name'}
    changed = client.post(source_path + '/versions', json={**body, 'content': body['content'] + '\n새 조항', 'expectedRevision': 1}, headers=headers)
    assert changed.status_code == 201
    assert client.delete(source_path, headers={**headers, 'If-Match': '"2"'}).status_code == 204
    assert client.get(source_path, headers=headers).status_code == 404
    assert client.get(path + '/evaluation-snapshot', headers=headers).json()['data'] == snapshot
    new = client.post(base + '/testsets/' + data['testSet']['id'] + '/versions',
        json={'sourceVersionId': data['version']['id'], 'expectedRevision': 1}, headers=headers)
    assert new.status_code == 201, new.text
    assert new.json()['data']['questions'][0]['reviewStatus'] == 'pending'
    assert new.json()['data']['version'] == 2


def test_scope_validation_and_revision_conflicts(db_and_client):
    """Reject cross-owner/cross-project references and preserve the draft on failed edits."""
    _, client = db_and_client
    headers, base, policy, source, data, path, question = setup(client)
    other = account(client, 'other')
    assert client.get(path, headers=other).status_code == 404
    assert client.get(base + '/policies', headers=other).status_code == 404
    assert client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=other).status_code == 404
    second = client.post('/api/projects', json={'name': 'Second'}, headers=headers).json()['data']['id']
    assert client.post(f'/api/projects/{second}/testsets', json={'name': 'Invalid', 'policyVersionIds': [source['version']['id']]}, headers=headers).status_code == 404
    bad = client.post(path + '/questions', json={**question, 'evidenceQuote': '없는 문장', 'expectedRevision': 1}, headers=headers)
    assert bad.status_code == 400
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
    added = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    question_path = added.headers['location']
    assert client.patch(question_path, json={**question, 'expectedRevision': 1}, headers=headers).status_code == 409
    assert client.delete(question_path, headers=headers).status_code == 428
    assert client.delete(question_path, headers={**headers, 'If-Match': '"1"'}).status_code == 412
    assert client.delete(question_path, headers={**headers, 'If-Match': '"2"'}).status_code == 204
    assert client.get(path, headers=headers).json()['data']['questions'] == []
    assert client.post(base + '/policies', json={**policy, 'rules': [policy['rules'][0], policy['rules'][0]]}, headers=headers).status_code == 400
    source_path = base + '/policies/' + source['policy']['id']
    assert client.delete(source_path, headers=headers).status_code == 428
    assert client.delete(source_path, headers={**headers, 'If-Match': '"2"'}).status_code == 412


class FakeGenerator:
    """A deterministic test-only adapter, never selected in the application default."""
    def __init__(self, question):
        """Keep call counts to verify idempotency prevents repeated model work."""
        self.question = question
        self.calls = 0

    def generate(self, *, policies, count, **kwargs):
        """Return structured candidates using the provided policy version."""
        self.calls += 1
        assert policies[0]['id'] == self.question['policyVersionId']
        return {'questions': [generated_question(self.question, f' ({index + 1})') for index in range(count)]}


def test_generation_is_unconfigured_and_fake_adapter_is_only_for_tests(db_and_client):
    """Missing provider never creates fake data; injected results still require human approval."""
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    h = {**headers, 'Idempotency-Key': 'batch-1'}
    missing = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 2}, headers=h)
    assert missing.status_code == 503 and missing.json()['error']['code'] == 'GENERATION_NOT_CONFIGURED'
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
    fake = FakeGenerator(question)
    app.dependency_overrides[get_question_generator] = lambda: fake
    body = {'expectedRevision': 1, 'count': 2, 'categoryCounts': {'normal': 2}}
    response = client.post(path + '/questions/generate', json=body, headers=h)
    assert response.status_code == 200, response.text
    assert len(response.json()['data']['questions']) == 2
    assert all(q['reviewStatus'] == 'pending' for q in response.json()['data']['questions'])
    assert client.post(path + '/questions/generate', json=body, headers=h).json() == response.json()
    assert fake.calls == 1
    assert client.post(path + '/questions/generate', json={**body, 'count': 1, 'categoryCounts': {'normal': 1}}, headers=h).status_code == 409
    assert client.post(path + '/confirm', json={'expectedRevision': 2}, headers=headers).status_code == 409


@pytest.mark.parametrize('failure,status', [('timeout', 504), ('broken', 503), ('untrusted', 502), ('count', 502)])
def test_generation_failure_is_atomic(db_and_client, failure, status):
    """Bad or unavailable model output cannot partially change draft revision or questions."""
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)

    class Failing:
        """Simulate provider failures without sending policy data outside the tests."""
        def generate(self, **kwargs):
            """Return wrong evidence or fail as an external model adapter might."""
            if failure == 'timeout':
                raise TimeoutError()
            if failure == 'broken':
                raise RuntimeError('api-key-must-not-leak')
            if failure == 'count':
                return {'questions': []}
            return {'questions': [{**question, 'evidenceQuote': '허위 근거'}]}

    app.dependency_overrides[get_question_generator] = lambda: Failing()
    response = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 1},
        headers={**headers, 'Idempotency-Key': 'failed-batch'})
    assert response.status_code == status, response.text
    assert 'api-key-must-not-leak' not in response.text
    version = client.get(path, headers=headers).json()['data']
    assert version['revision'] == 1 and version['questions'] == []


def test_policy_history_pagination_and_new_policy_sources(db_and_client):
    """A new draft can adopt new source versions without silently reusing obsolete questions."""
    _, client = db_and_client
    headers, base, policy, source, data, path, question = setup(client)
    ppath = base + '/policies/' + source['policy']['id']
    content = {key: value for key, value in policy.items() if key != 'name'}
    updated = client.post(ppath + '/versions', json={**content, 'expectedRevision': 1}, headers=headers).json()['data']
    assert client.post(ppath + '/versions', json={**content, 'expectedRevision': 1}, headers=headers).status_code == 409
    first = client.get(ppath + '/versions', params={'limit': 1}, headers=headers).json()['data']
    assert first['items'][0]['version'] == 2
    second = client.get(ppath + '/versions', params={'limit': 1, 'cursor': first['nextCursor']}, headers=headers).json()['data']
    assert second['items'][0]['content'] == policy['content'] and second['nextCursor'] is None
    assert client.get(base + '/testsets', params={'cursor': first['nextCursor']}, headers=headers).status_code == 400
    added = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    qpath = added.headers['location']
    client.post(qpath + '/review', json={'expectedRevision': 2, 'status': 'approved'}, headers=headers)
    assert client.post(path + '/confirm', json={'expectedRevision': 3}, headers=headers).status_code == 200
    new = client.post(base + '/testsets/' + data['testSet']['id'] + '/versions', json={
        'sourceVersionId': data['version']['id'], 'expectedRevision': 1, 'policyVersionIds': [updated['id']]}, headers=headers)
    assert new.status_code == 201, new.text
    assert new.json()['data']['policyVersionIds'] == [updated['id']]
    assert new.json()['data']['questions'] == []


def test_rejected_questions_capacity_and_api_contract(db_and_client, monkeypatch):
    """Rejected or over-limit drafts cannot become evaluator inputs, and Swagger describes the contract."""
    from app.api import testsets
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    monkeypatch.setattr(testsets, 'MAX_QUESTIONS', 1)
    added = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    qpath = added.headers['location']
    assert client.post(path + '/questions', json={**question, 'expectedRevision': 2}, headers=headers).status_code == 400
    assert client.get(path, headers=headers).json()['data']['revision'] == 2
    reviewed = client.post(qpath + '/review', json={'expectedRevision': 2, 'status': 'rejected', 'note': '기대 행동 재검토'}, headers=headers)
    assert reviewed.status_code == 200
    assert client.post(path + '/confirm', json={'expectedRevision': 3}, headers=headers).status_code == 409
    assert client.get(path + '/evaluation-snapshot', headers=headers).status_code == 409
    schema = client.get('/openapi.json').json()
    assert 'EvaluationSnapshot' in schema['components']['schemas']
    assert 'QuestionWrite' in schema['components']['schemas']
    cors = client.options(qpath, headers={'Origin': 'http://localhost:5173',
        'Access-Control-Request-Method': 'PATCH', 'Access-Control-Request-Headers': 'authorization,content-type,if-match,idempotency-key'})
    assert cors.status_code == 200


def test_generation_hydrates_server_owned_policy_evidence(db_and_client):
    """The server, not the model, supplies policy versions and verbatim source quotes."""
    _, client = db_and_client
    headers, _, _, source, _, path, question = setup(client)
    semantic = generated_question(question)

    class Slipping:
        def generate(self, *, policies, count, **kwargs):
            return {'questions': [deepcopy(semantic) for _ in range(count)]}

    app.dependency_overrides[get_question_generator] = lambda: Slipping()
    response = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 1},
                           headers={**headers, 'Idempotency-Key': 'slip-1'})
    assert response.status_code == 200, response.text
    saved = response.json()['data']['questions'][0]
    assert saved['policyVersionId'] == source['version']['id']
    assert saved['evidenceQuote'] == '예약 취소는 출발 24시간 전까지 가능합니다.'
    assert saved['ruleKeys'] == ['refund']
    assert saved['expectedConditions'][0]['policyVersionId'] == source['version']['id']
    assert saved['expectedConditions'][0]['evidenceQuote'] == '예약 취소는 출발 24시간 전까지 가능합니다.'


def test_human_questions_are_not_repaired(db_and_client):
    """Only generated drafts are repaired; a person must still pick a real version and quote."""
    _, client = db_and_client
    headers, _, _, source, _, path, question = setup(client)
    wrong_id = client.post(path + '/questions', json={**question, 'policyVersionId': source['policy']['id'], 'expectedRevision': 1}, headers=headers)
    assert wrong_id.status_code == 400
    wrong_quote = client.post(path + '/questions', json={**question, 'evidenceQuote': '취소는 24시간 전까지', 'expectedRevision': 1}, headers=headers)
    assert wrong_quote.status_code == 400


def test_generation_still_rejects_only_unknown_rules(db_and_client):
    """If none of the cited rule keys exist the generated batch is rejected as before."""
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)

    class Unknown:
        def generate(self, *, policies, count, **kwargs):
            generated = generated_question(question)
            generated['ruleKeys'] = ['MADE_UP_RULE']
            generated['expectedConditions'][0]['ruleKeys'] = ['MADE_UP_RULE']
            return {'questions': [generated]}

    app.dependency_overrides[get_question_generator] = lambda: Unknown()
    response = client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': 1},
                           headers={**headers, 'Idempotency-Key': 'unknown-1'})
    assert response.status_code == 502 and response.json()['error']['code'] == 'GENERATION_EVIDENCE_INVALID'
