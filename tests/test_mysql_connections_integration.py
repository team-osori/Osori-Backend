"""Opt-in MySQL/MariaDB transactional tests against a disposable database only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.connection_worker import process_one
from app.models.connection import ConnectionTest
from app.models.prompt import PromptVersion
from test_connections_prompts import CONFIG
from test_mysql_auth_integration import mysql_client, signup_and_login  # noqa: F401


def resources(client):
    user = signup_and_login(client)
    headers = {'Authorization': 'Bearer ' + user['accessToken']}
    project = client.post('/api/projects', headers=headers, json={'name': '통합검증'}).headers['location']
    service = client.post(project + '/services', headers=headers, json={'name': '챗봇'}).headers['location']
    prompt = client.post(service + '/prompts', headers=headers,
                         json={'name': '프롬프트', 'content': '한' * 100_000})
    assert prompt.status_code == 201, prompt.text
    return headers, service, prompt.json()['data']


def concurrently(action):
    barrier = Barrier(2)

    def run(i):
        barrier.wait(timeout=10)
        return action(i)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, i) for i in range(2)]
        return [f.result(timeout=20) for f in futures]


def test_mysql_concurrent_project_service_creation(mysql_client):
    from app.models.project import Chatbot, Project
    client, sessions, _, _ = mysql_client
    user = signup_and_login(client)
    headers = {'Authorization': 'Bearer ' + user['accessToken'], 'Idempotency-Key': 'project-wizard'}
    body = {'name': '동시 생성', 'service': {'name': '기업 챗봇'}}
    responses = concurrently(lambda _: client.post('/api/projects', headers=headers, json=body))
    assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
    assert len({r.json()['data']['id'] for r in responses}) == 1
    project_id = responses[0].json()['data']['id']
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Project).where(Project.owner_id == user['user']['id'])) == 1
        assert db.scalar(select(func.count()).select_from(Chatbot).where(Chatbot.project_id == project_id)) == 1
    conflict = client.post('/api/projects', headers=headers, json={'name': '다른 요청'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_mysql_project_list_search_counts_and_summary(mysql_client):
    from uuid import uuid4
    client, _, _, _ = mysql_client
    user = signup_and_login(client)
    headers = {'Authorization': 'Bearer ' + user['accessToken']}
    token = uuid4().hex
    for index in range(3):
        response = client.post('/api/projects', headers=headers,
            json={'name': f'{token}-{index}', 'service': {'name': '챗봇'}})
        assert response.status_code == 201, response.text
    first = client.get('/api/projects', headers=headers,
        params={'search': token, 'status': 'SETUP', 'limit': 2})
    assert first.status_code == 200, first.text
    page = first.json()['data']
    assert page['total'] == 3 and page['statusCounts'] == {'SETUP': 3, 'RUNNING': 0, 'DONE': 0}
    assert page['items'][0]['service']['name'] == '챗봇'
    second = client.get('/api/projects', headers=headers,
        params={'search': token, 'status': 'SETUP', 'cursor': page['nextCursor']}).json()['data']
    assert len(second['items']) == 1 and second['nextCursor'] is None


def test_mysql_concurrent_prompt_versions(mysql_client):
    client, sessions, _, _ = mysql_client
    headers, service, prompt = resources(client)
    path = service + '/prompts/' + prompt['id']
    responses = concurrently(lambda i: client.post(path + '/versions', headers=headers,
        json={'baseVersionId': prompt['currentVersionId'], 'content': f'수정본{i}'}))
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(PromptVersion).where(PromptVersion.prompt_id == prompt['id'])) == 2
    latest = client.get(path, headers=headers).json()['data']
    assert latest['revision'] == 2 and latest['currentVersion']['version'] == 2
    assert client.get(path + '/versions/' + prompt['currentVersionId'], headers=headers).json()['data']['content'] == '한' * 100_000


def test_mysql_concurrent_connection_creation_and_secret_rotation(mysql_client, monkeypatch):
    client, _, _, _ = mysql_client
    monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    headers, service, _ = resources(client)
    responses = concurrently(lambda _: client.put(service + '/connection', headers=headers, json=CONFIG))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    responses = concurrently(lambda i: client.put(service + '/connection/secret', headers=headers,
        json={'expectedRevision': 1, 'secret': f'test-only-secret-{i}'}))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    assert client.get(service + '/connection', headers=headers).json()['data']['revision'] == 2


def test_mysql_idempotency_and_worker_claim(mysql_client):
    client, sessions, _, _ = mysql_client

    async def finish_previous_test(*args):
        return 'previous test completed'

    # This opt-in DB retains test history across runs. Drain only with a fake sender.
    while asyncio.run(process_one(sessions, sender=finish_previous_test)):
        pass
    headers, service, prompt = resources(client)
    assert client.put(service + '/connection', headers=headers,
                      json={**CONFIG, 'auth': {'type': 'NONE'}}).status_code == 200
    body = {'connectionRevision': 1, 'question': 'Q', 'promptVersionId': prompt['currentVersionId']}
    responses = concurrently(lambda _: client.post(service + '/connection-tests',
        headers={**headers, 'Idempotency-Key': 'same-key'}, json=body))
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    ids = {r.json()['data']['id'] for r in responses}
    assert len(ids) == 1
    calls = []

    async def sender(*args):
        calls.append(1)
        await asyncio.sleep(0.05)
        return 'answer'

    concurrently(lambda _: asyncio.run(process_one(sessions, sender=sender)))
    assert len(calls) == 1
    with sessions() as db:
        job = db.get(ConnectionTest, next(iter(ids)))
        assert job.status == 'SUCCEEDED' and job.attempt_count == 1


def test_mysql_idempotency_conflict(mysql_client):
    client, _, _, _ = mysql_client
    headers, service, prompt = resources(client)
    client.put(service + '/connection', headers=headers, json={**CONFIG, 'auth': {'type': 'NONE'}})
    responses = concurrently(lambda i: client.post(service + '/connection-tests',
        headers={**headers, 'Idempotency-Key': 'same-key'}, json={
            'connectionRevision': 1, 'question': f'Q{i}', 'promptVersionId': prompt['currentVersionId']}))
    assert sorted(r.status_code for r in responses) == [202, 409], [r.text for r in responses]
