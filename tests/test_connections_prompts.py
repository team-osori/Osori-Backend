import asyncio
from copy import deepcopy
from datetime import timedelta

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from app.connection_worker import expire_abandoned_jobs, process_one
from app.core.errors import ApiError
from app.models.connection import Connection, ConnectionTest
from app.models.prompt import PromptVersion
from app.services.auth import now
from app.services.chatbot_gateway import GatewayError
from app.services.prompts import select_ab_versions
from test_projects import register


CONFIG = {'expectedRevision': 0, 'executionMode': 'PROMPT_BODY', 'method': 'POST',
          'url': 'https://chatbot.example.com/chat', 'headers': {'Content-Type': 'application/json'},
          'requestTemplate': {'messages': [{'role': 'system', 'content': ''},
                                            {'role': 'user', 'content': ''}]},
          'questionPointer': '/messages/1/content', 'promptPointer': '/messages/0/content',
          'answerPointer': '/answer', 'auth': {'type': 'BEARER'},
          'timeoutMs': 1000, 'maxRetries': 0, 'retrySafe': False}


@pytest.fixture
def setup(db_and_client, monkeypatch):
    factory, client = db_and_client
    monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    headers = register(client, 'owner')
    project = client.post('/api/projects', headers=headers, json={'name': '항공'}).headers['location']
    service = client.post(project + '/services', headers=headers, json={'name': '챗봇'}).headers['location']
    prompt = client.post(service + '/prompts', headers=headers,
                         json={'name': '원문', 'content': '  원문\n공백 보존\n'}).json()['data']
    return factory, client, headers, service, prompt


def prepare_connection(client, headers, service, auth=True):
    config = deepcopy(CONFIG)
    if not auth:
        config['auth'] = {'type': 'NONE'}
    response = client.put(service + '/connection', headers=headers, json=config)
    assert response.status_code == 200, response.text
    if auth:
        response = client.put(service + '/connection/secret', headers=headers,
                              json={'expectedRevision': 1, 'secret': 'test-only-secret-token'})
        assert response.status_code == 200, response.text
    return 2 if auth else 1


def enqueue(client, headers, service, prompt, revision=2, key='test-1', **extra):
    body = {'connectionRevision': revision, 'question': '질문',
            'promptVersionId': prompt['currentVersionId'], **extra}
    return client.post(service + '/connection-tests', headers={**headers, 'Idempotency-Key': key}, json=body)


def test_prompt_versions_are_immutable_and_selectable(setup):
    factory, client, headers, service, prompt = setup
    path = service + '/prompts/' + prompt['id']
    first = prompt['currentVersion']
    assert first['content'] == '  원문\n공백 보존\n'
    response = client.post(path + '/versions', headers=headers, json={
        'baseVersionId': first['id'], 'content': '수정본', 'label': 'B', 'changeNote': '정책 강화'})
    assert response.status_code == 201
    second = response.json()['data']
    assert second['version'] == 2
    assert client.get(response.headers['location'], headers=headers).json()['data'] == second
    assert client.get(path + '/versions/' + first['id'], headers=headers).json()['data'] == first
    latest = client.get(path, headers=headers).json()['data']
    assert latest['revision'] == 2 and latest['currentVersionId'] == second['id']
    versions = client.get(path + '/versions', headers=headers, params={'limit': 1}).json()['data']
    assert versions['items'][0]['id'] == second['id'] and 'content' not in versions['items'][0]
    next_page = client.get(path + '/versions', headers=headers,
                          params={'limit': 1, 'cursor': versions['nextCursor']}).json()['data']
    assert next_page['items'][0]['id'] == first['id'] and next_page['nextCursor'] is None
    stale = client.post(path + '/versions', headers=headers,
                        json={'baseVersionId': first['id'], 'content': '다른 수정본'})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'PROMPT_VERSION_CONFLICT'
    unchanged = client.post(path + '/versions', headers=headers,
                            json={'baseVersionId': second['id'], 'content': '수정본', 'label': '이름만 변경'})
    assert unchanged.json()['error']['code'] == 'NO_CHANGES'
    another = client.post(service + '/prompts', headers=headers,
                          json={'name': '다른 지시문', 'content': '내용'}).json()['data']
    with factory() as db:
        a, b = select_ab_versions(db, prompt['serviceId'], first['id'], second['id'])
        assert a.content == first['content'] and b.content == '수정본'
        for ids, code in [((first['id'], first['id']), 'SAME_PROMPT_VERSION'),
                          ((first['id'], another['currentVersionId']), 'PROMPT_SCOPE_MISMATCH')]:
            with pytest.raises(ApiError) as error:
                select_ab_versions(db, prompt['serviceId'], *ids)
            assert error.value.code == code
        assert len(list(db.scalars(select(PromptVersion)))) == 3


def test_prompt_validation_scope_and_pagination(setup):
    _, client, headers, service, prompt = setup
    other = register(client, 'other')
    for suffix in ('/prompts', '/prompts/' + prompt['id'], '/prompts/' + prompt['id'] + '/versions'):
        assert client.get(service + suffix).status_code == 401
        assert client.get(service + suffix, headers=other).status_code == 404
    for content in ('', ' \n', 'x' * 100_001):
        assert client.post(service + '/prompts', headers=headers,
                           json={'name': 'name', 'content': content}).status_code == 400
    long_content = '한' * 100_000
    assert client.post(service + '/prompts', headers=headers,
                       json={'name': '최대 길이', 'content': long_content}).status_code == 201
    page = client.get(service + '/prompts', headers=headers, params={'limit': 1}).json()['data']
    assert page['nextCursor']
    assert client.get(service + '/prompts', headers=headers,
                      params={'cursor': page['nextCursor']}).json()['data']['items']
    for suffix in ('/prompts', '/prompts/' + prompt['id'] + '/versions'):
        for cursor in ('broken', 'W10=', 'bnVsbA=='):
            assert client.get(service + suffix, headers=headers, params={'cursor': cursor}).status_code == 400
    another = client.post(service + '/prompts', headers=headers,
                          json={'name': '다른 지시문', 'content': '내용'}).json()['data']
    assert client.get(service + '/prompts/' + prompt['id'] + '/versions/' + another['currentVersionId'],
                      headers=headers).status_code == 404


def test_connection_secret_lifecycle_encryption_and_revision(setup, caplog):
    factory, client, headers, service, _ = setup
    assert client.get(service + '/connection', headers=headers).status_code == 404
    prepare_connection(client, headers, service)
    assert client.get(service, headers=headers).json()['data']['hasConnection'] is True
    assert client.get(service.rsplit('/', 1)[0], headers=headers).json()['data']['items'][0]['hasConnection'] is True
    data = client.get(service + '/connection', headers=headers)
    assert 'test-only-secret-token' not in data.text
    assert data.json()['data']['auth']['secretConfigured'] is True
    with factory() as db:
        row = db.scalar(select(Connection))
        assert 'test-only-secret-token' not in row.secret_ciphertext
        row.verified_revision = row.revision
        db.commit()
    assert client.put(service + '/connection', headers=headers, json=CONFIG).status_code == 409
    assert client.put(service + '/connection/secret', headers=headers,
        json={'expectedRevision': 1, 'secret': 'next-secret'}).status_code == 409
    updated = client.put(service + '/connection', headers=headers, json={**CONFIG, 'expectedRevision': 2})
    assert updated.json()['data']['auth']['secretConfigured'] is True
    assert updated.json()['data']['verifiedRevision'] is None
    changed = client.put(service + '/connection', headers=headers,
        json={**CONFIG, 'expectedRevision': 3, 'url': 'https://other.example.com/chat'})
    assert changed.json()['data']['auth']['secretConfigured'] is False
    assert client.delete(service + '/connection/secret', headers=headers).status_code == 428
    assert client.delete(service + '/connection/secret', headers={**headers, 'If-Match': '4'}).status_code == 400
    assert client.delete(service + '/connection/secret', headers={**headers, 'If-Match': '"2"'}).status_code == 412
    assert client.delete(service + '/connection/secret', headers={**headers, 'If-Match': '"4"'}).status_code == 204
    assert 'test-only-secret-token' not in caplog.text


def test_secret_validation_fail_closed_and_no_auth(setup, monkeypatch):
    _, client, headers, service, _ = setup
    prepare_connection(client, headers, service, auth=False)
    assert client.put(service + '/connection/secret', headers=headers,
        json={'expectedRevision': 1, 'secret': 'secret'}).json()['error']['code'] == 'AUTH_NOT_REQUIRED'
    client.put(service + '/connection', headers=headers, json={**CONFIG, 'expectedRevision': 1})
    for value in ('', '  ', 'secret\r\nX-Test: injected', 'x' * 8193):
        r = client.put(service + '/connection/secret', headers=headers,
                       json={'expectedRevision': 2, 'secret': value})
        assert r.status_code == 400
        assert 'injected' not in r.text
    monkeypatch.delenv('CONNECTION_ENCRYPTION_KEY')
    response = client.put(service + '/connection/secret', headers=headers,
                           json={'expectedRevision': 2, 'secret': 'sensitive-secret'})
    assert response.status_code == 503 and 'sensitive-secret' not in response.text


@pytest.mark.parametrize('change', [
    {'questionPointer': '/missing'}, {'promptPointer': '/messages'}, {'promptPointer': '/messages/1/content'},
    {'answerPointer': 'answer'}, {'questionPointer': '/bad~2token'}, {'maxRetries': 1},
    {'timeoutMs': 0}, {'headers': {'Authorization': 'secret'}}, {'headers': {'Host': 'localhost'}},
    {'headers': {'X-Test': 'x\r\ny'}}, {'headers': {'x-api-key': 'secret'}},
    {'headers': {'content-type': 'text/plain'}}, {'headers': {'a': 'b', 'A': 'c'}},
    {'auth': {'type': 'HEADER', 'headerName': 'Host'}}, {'method': 'GET'},
])
def test_invalid_connection_configuration(setup, change):
    _, client, headers, service, _ = setup
    assert client.put(service + '/connection', headers=headers, json={**CONFIG, **change}).status_code == 400


def test_idempotent_queue_and_worker_success(setup):
    factory, client, headers, service, prompt = setup
    prepare_connection(client, headers, service)
    response = enqueue(client, headers, service, prompt)
    assert response.status_code == 202, response.text
    assert response.json()['data']['status'] == 'QUEUED'
    assert enqueue(client, headers, service, prompt).json()['data']['id'] == response.json()['data']['id']
    assert enqueue(client, headers, service, prompt, question='다른 질문').json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    calls = []

    async def sender(config, payload, outgoing_headers):
        calls.append(payload)
        assert outgoing_headers['Authorization'] == 'Bearer test-only-secret-token'
        assert payload['messages'][0]['content'] == prompt['currentVersion']['content']
        assert payload['messages'][1]['content'] == '질문'
        return '답변 test-only-secret-token'

    assert asyncio.run(process_one(factory, sender=sender))
    assert not asyncio.run(process_one(factory, sender=sender))
    result = client.get(response.headers['location'], headers=headers).json()['data']
    assert result['status'] == 'SUCCEEDED' and result['answer'] == '답변 [REDACTED]'
    assert result['attemptCount'] == 1 and result['appliedVersionVerified'] is False
    assert result['finishedAt'] and result['latencyMs'] >= 0 and len(calls) == 1
    assert client.get(service + '/connection', headers=headers).json()['data']['verifiedRevision'] == 2
    assert enqueue(client, headers, service, prompt).json()['data']['status'] == 'SUCCEEDED'


@pytest.mark.parametrize('code', ['UPSTREAM_TIMEOUT', 'UPSTREAM_AUTH_FAILED', 'INVALID_UPSTREAM_RESPONSE'])
def test_worker_failures_are_execution_failures(setup, code):
    factory, client, headers, service, prompt = setup
    prepare_connection(client, headers, service)
    response = enqueue(client, headers, service, prompt)

    async def failed(*args):
        if code == 'UPSTREAM_TIMEOUT':
            raise TimeoutError('sensitive-exception')
        raise GatewayError(code, '안전한 오류 메시지')

    asyncio.run(process_one(factory, sender=failed))
    result = client.get(response.headers['location'], headers=headers)
    assert result.json()['data']['status'] == 'FAILED'
    assert result.json()['data']['error']['code'] == code
    assert result.json()['data']['answer'] is None
    assert 'sensitive-exception' not in result.text
    assert client.get(service + '/connection', headers=headers).json()['data']['verifiedRevision'] is None


def test_queued_configuration_change_and_abandoned_job(setup):
    factory, client, headers, service, prompt = setup
    prepare_connection(client, headers, service)
    response = enqueue(client, headers, service, prompt)
    client.put(service + '/connection', headers=headers, json={**CONFIG, 'expectedRevision': 2})

    async def never_called(*args):
        pytest.fail('Changed connection must not be called')

    asyncio.run(process_one(factory, sender=never_called))
    assert client.get(response.headers['location'], headers=headers).json()['data']['error']['code'] == 'CONNECTION_CHANGED'
    second = enqueue(client, headers, service, prompt, revision=3, key='second')
    with factory() as db:
        job = db.get(ConnectionTest, second.json()['data']['id'])
        job.status = 'RUNNING'
        job.started_at = now() - timedelta(minutes=10)
        db.commit()
    expire_abandoned_jobs(factory)
    assert client.get(second.headers['location'], headers=headers).json()['data']['error']['code'] == 'WORKER_INTERRUPTED'
    assert not asyncio.run(process_one(factory, sender=never_called))


def test_revision_change_during_call_does_not_verify_new_configuration(setup):
    factory, client, headers, service, prompt = setup
    prepare_connection(client, headers, service)
    response = enqueue(client, headers, service, prompt)

    async def sender(*args):
        assert client.put(service + '/connection', headers=headers,
                          json={**CONFIG, 'expectedRevision': 2}).status_code == 200
        return 'old configuration answer'

    asyncio.run(process_one(factory, sender=sender))
    assert client.get(response.headers['location'], headers=headers).json()['data']['status'] == 'SUCCEEDED'
    assert client.get(service + '/connection', headers=headers).json()['data']['verifiedRevision'] is None


def test_completed_idempotency_key_expires_without_deleting_history(setup):
    factory, client, headers, service, prompt = setup
    prepare_connection(client, headers, service)
    response = enqueue(client, headers, service, prompt)
    first_id = response.json()['data']['id']
    with factory() as db:
        row = db.get(ConnectionTest, first_id)
        row.created_at = now() - timedelta(hours=25)
        db.commit()
    # Queued jobs retain their key even after 24 hours.
    assert enqueue(client, headers, service, prompt).json()['data']['id'] == first_id
    with factory() as db:
        row = db.get(ConnectionTest, first_id)
        row.status = 'SUCCEEDED'
        row.answer = 'preserved'
        row.finished_at = now()
        db.commit()
    assert enqueue(client, headers, service, prompt).json()['data']['id'] != first_id
    assert client.get(response.headers['location'], headers=headers).json()['data']['answer'] == 'preserved'


def test_test_submission_validation_and_ownership(setup):
    _, client, headers, service, prompt = setup
    prepare_connection(client, headers, service, auth=False)
    assert enqueue(client, headers, service, prompt).json()['error']['code'] == 'CONNECTION_CHANGED'
    assert enqueue(client, headers, service, prompt, revision=1, promptVersionId=None).status_code == 400
    assert enqueue(client, headers, service, prompt, revision=1, promptVersionId='missing').status_code == 404
    assert client.post(service + '/connection-tests', headers=headers,
                       json={'connectionRevision': 1, 'question': 'q'}).status_code == 400
    response = enqueue(client, headers, service, prompt, revision=1)
    other = register(client, 'other')
    for path in (service + '/connection', response.headers['location']):
        assert client.get(path, headers=other).status_code == 404
    assert enqueue(client, other, service, prompt, revision=1).status_code == 404
    project2 = client.post('/api/projects', headers=headers, json={'name': 'second'}).headers['location']
    service2 = client.post(project2 + '/services', headers=headers, json={'name': 'other'}).headers['location']
    prepare_connection(client, headers, service2, auth=False)
    assert enqueue(client, headers, service2, prompt, revision=1).status_code == 404
    assert client.get(service2 + '/connection-tests/' + response.json()['data']['id'], headers=headers).status_code == 404
    assert client.get(service2 + '/prompts/' + prompt['id'], headers=headers).status_code == 404
