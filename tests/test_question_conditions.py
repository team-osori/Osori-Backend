from copy import deepcopy

from tests.test_policy_testsets import account, setup


def test_condition_ids_review_history_and_snapshot(db_and_client):
    _, client = db_and_client
    headers, base, _, _, data, path, question = setup(client)
    condition = {'description': '취소 가능 여부를 안내한다.', 'kind': 'required', 'category': 'policy',
                 **{k: question[k] for k in ('policyVersionId', 'ruleKeys', 'evidenceQuote')}}
    body = {**question, 'category': 'boundary', 'expectedConditions': [condition], 'expectedRevision': 1}
    added = client.post(path + '/questions', json=body, headers=headers)
    assert added.status_code == 201, added.text
    qpath = added.headers['location']
    q = added.json()['data']['questions'][0]
    cid = q['expectedConditions'][0]['id']
    assert cid and q['expectedBehavior'] == condition['description'] and q['forbiddenBehaviors'] == []
    reviewed = client.post(qpath + '/review', json={'expectedRevision': 2, 'status': 'approved', 'note': '정책 확인'}, headers=headers)
    assert reviewed.json()['data']['questions'][0]['reviewedAt']
    edited = client.patch(qpath, json={**body, 'expectedRevision': 3, 'question': '출발 정확히 24시간 전이면요?',
        'expectedConditions': [{**condition, 'id': cid}], 'changeNote': '경계값 구체화'}, headers=headers)
    assert edited.status_code == 200, edited.text
    after = edited.json()['data']['questions'][0]
    assert after['reviewStatus'] == 'pending' and after['reviewedAt'] is None
    assert after['expectedConditions'][0]['id'] == cid
    history = client.get(qpath + '/history', headers=headers).json()['data']['items']
    assert [h['action'] for h in history] == ['edited', 'reviewed', 'created']
    assert history[0]['before']['reviewStatus'] == 'approved'
    assert history[0]['after'] == after
    assert history[0]['note'] == '경계값 구체화'
    assert history[0]['actorId'] and history[0]['createdAt']
    first = client.get(qpath + '/history?limit=1', headers=headers).json()['data']
    second = client.get(qpath + '/history', params={'limit': 1, 'cursor': first['nextCursor']}, headers=headers).json()['data']
    assert second['items'][0]['action'] == 'reviewed'
    assert client.get(path + '/history', params={'cursor': first['nextCursor']}, headers=headers).status_code == 400
    other = account(client, 'other')
    assert client.get(qpath + '/history', headers=other).status_code == 404
    client.post(qpath + '/review', json={'expectedRevision': 4, 'status': 'approved'}, headers=headers)
    assert client.post(path + '/confirm', json={'expectedRevision': 5}, headers=headers).status_code == 200
    snapshot = client.get(path + '/evaluation-snapshot', headers=headers).json()['data']
    assert snapshot['schemaVersion'] == 2 and snapshot['questions'][0]['expectedConditions'][0]['id'] == cid
    copied = client.post(base + '/testsets/' + data['testSet']['id'] + '/versions', json={
        'sourceVersionId': data['version']['id'], 'expectedRevision': 1}, headers=headers)
    assert copied.status_code == 201, copied.text
    clone = copied.json()['data']['questions'][0]
    assert clone['id'] != q['id'] and clone['expectedConditions'][0]['id'] == cid
    assert clone['reviewStatus'] == 'pending'
    assert client.get(path + '/evaluation-snapshot', headers=headers).json()['data'] == snapshot


def test_invalid_conditions_are_atomic_and_deleted_history_survives(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    added = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    qpath = added.headers['location']
    conditions = added.json()['data']['questions'][0]['expectedConditions']
    assert len(conditions) == 2
    for change in ({'id': 'invented-id'}, {'policyVersionId': 'another-project'},
                   {'evidenceQuote': '정책에 없는 내용'}, {'ruleKeys': ['refund', 'refund']}):
        invalid = deepcopy(conditions)
        invalid[0].update(change)
        response = client.patch(qpath, json={**question, 'expectedRevision': 2, 'expectedConditions': invalid}, headers=headers)
        assert response.status_code == 400, response.text
        assert client.get(path, headers=headers).json()['data']['revision'] == 2
        assert len(client.get(qpath + '/history', headers=headers).json()['data']['items']) == 1
    reviewed = client.post(qpath + '/review', json={'expectedRevision': 2, 'status': 'rejected', 'note': '근거 보완'}, headers=headers)
    assert reviewed.status_code == 200
    assert client.delete(qpath, headers={**headers, 'If-Match': '"3"'}).status_code == 204
    assert client.get(qpath, headers=headers).status_code == 404
    events = client.get(qpath + '/history', headers=headers).json()['data']['items']
    assert events[0]['action'] == 'deleted' and events[0]['after'] is None
    assert events[0]['before']['reviewStatus'] == 'rejected'
    assert client.get(path + '/questions/missing/history', headers=headers).status_code == 404


def test_generation_categories_are_validated_and_failed_calls_are_limited(db_and_client, monkeypatch):
    from app.main import app
    from app.services.question_generation import get_question_generator
    monkeypatch.setattr('app.services.rate_limit.time.time', lambda: 1800000000)
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    calls = []

    class Generator:
        def generate(self, *, policies, count, categories):
            calls.append(categories)
            return {'questions': [{**question, 'category': category} for category in categories]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    h = {**headers, 'Idempotency-Key': 'categories'}
    for categories, count in [(['boundary', 'boundary'], 2), (['boundary', 'normal'], 1)]:
        assert client.post(path + '/questions/generate', json={'expectedRevision': 1, 'count': count, 'categories': categories}, headers=h).status_code == 400
    body = {'expectedRevision': 1, 'count': 3, 'categories': ['normal', 'exception', 'boundary']}
    result = client.post(path + '/questions/generate', json=body, headers=h)
    assert result.status_code == 200, result.text
    assert calls == [body['categories']]
    assert len(client.get(path + '/history', headers=headers).json()['data']['items']) == 3
    assert client.post(path + '/questions/generate', json=body, headers=h).json() == result.json()
    assert len(calls) == 1
    for i in range(8):
        response = client.post(path + '/questions/generate', json={**body, 'expectedRevision': 1},
            headers={**headers, 'Idempotency-Key': str(i)})
        assert response.status_code == 409
    limited = client.post(path + '/questions/generate', json=body, headers=h)
    assert limited.status_code == 429 and int(limited.headers['retry-after']) > 0
