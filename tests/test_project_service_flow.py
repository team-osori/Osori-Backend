import asyncio
from sqlalchemy import event, func, select

from app.connection_worker import process_one
from app.models.auth import User
from app.models.project import Chatbot, Project, ProjectMember
from app.services.auth import now
from test_projects import register
from test_connections_prompts import CONFIG
from test_single_evaluation import single_ready  # noqa: F401
from test_condition_evaluation import ConditionJudge, run_condition


def test_atomic_creation_retry_and_conflict(db_and_client):
    factory, client = db_and_client
    owner = register(client, 'flow_owner')
    other = register(client, 'flow_other')
    headers = {**owner, 'Idempotency-Key': 'wizard-attempt-1'}
    body = {'name': ' Project ', 'service': {'name': ' Bot '}}
    first = client.post('/api/projects', headers=headers, json=body)
    assert first.status_code == 201, first.text
    path = first.headers['location']
    assert first.json()['data']['service']['name'] == 'Bot'
    assert first.json()['data']['serviceCount'] == 1
    # A lost reply can be retried without a duplicate, even after metadata changes.
    assert client.patch(path, headers=owner, json={'expectedRevision': 1, 'name': 'Updated'}).status_code == 200
    retry = client.post('/api/projects', headers=headers, json=body)
    assert retry.status_code == 201 and retry.headers['location'] == path
    assert retry.json()['data']['name'] == 'Updated'
    conflict = client.post('/api/projects', headers=headers, json={'name': 'Other'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert client.post('/api/projects', headers={**other, 'Idempotency-Key': 'wizard-attempt-1'},
        json=body).headers['location'] != path
    assert client.get(path, headers=other).status_code == 404
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == 2
        assert db.scalar(select(func.count()).select_from(Chatbot)) == 2
        row = db.get(Project, path.rsplit('/', 1)[1])
        row.deleted_at = now()
        db.commit()
    deleted = client.post('/api/projects', headers=headers, json=body)
    assert deleted.status_code == 409 and deleted.json()['error']['code'] == 'PROJECT_DELETED'


def test_service_insert_failure_rolls_back_entire_creation(db_and_client):
    factory, client = db_and_client
    headers = register(client, 'rollback_owner')

    def reject_service(*args):
        raise RuntimeError('simulated service write failure')

    event.listen(Chatbot, 'before_insert', reject_service)
    try:
        response = client.post('/api/projects', headers=headers,
            json={'name': 'Rollback', 'service': {'name': 'Bot'}})
        assert response.status_code == 500
    finally:
        event.remove(Chatbot, 'before_insert', reject_service)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == 0
        assert db.scalar(select(func.count()).select_from(Chatbot)) == 0


def test_search_counts_pagination_and_access(db_and_client):
    factory, client = db_and_client
    headers = register(client, 'search_owner')
    other = register(client, 'search_other')
    paths = [client.post('/api/projects', headers=headers, json={'name': name}).headers['location']
        for name in ('Match 1', 'match 2', 'Match %_', 'Hidden')]
    shared = client.post('/api/projects', headers=other, json={'name': 'Match shared'}).headers['location']
    client.post('/api/projects', headers=other, json={'name': 'Match private'})
    with factory() as db:
        member = db.scalar(select(User).where(User.email == 'search_owner@example.com'))
        row = db.get(Project, shared.rsplit('/', 1)[1])
        db.add(ProjectMember(id='m', project_id=row.id, user_id=member.id, invited_by=row.owner_id,
            role='member', status='active', revision=1, created_at=now()))
        db.get(Project, paths[-1].rsplit('/', 1)[1]).deleted_at = now()
        db.commit()
    found, cursor = [], None
    while True:
        params = {'search': ' match ', 'status': 'SETUP', 'limit': 1}
        if cursor:
            params['cursor'] = cursor
        response = client.get('/api/projects', headers=headers, params=params)
        assert response.status_code == 200, response.text
        page = response.json()['data']
        assert page['total'] == 4 and page['statusCounts'] == {'SETUP': 4, 'RUNNING': 0, 'DONE': 0}
        found.extend(p['id'] for p in page['items'])
        cursor = page['nextCursor']
        if not cursor:
            break
        for changed in ({'search': 'different', 'status': 'SETUP'}, {'search': 'match', 'status': 'DONE'}, {}):
            assert client.get('/api/projects', headers=headers,
                params={**changed, 'cursor': cursor}).status_code == 400
    assert len(set(found)) == 4
    assert len(client.get('/api/projects', headers=headers, params={'search': '%_'}).json()['data']['items']) == 1
    assert client.get('/api/projects', headers=headers, params={'search': ' '}).status_code == 400
    assert client.get('/api/projects', headers=headers, params={'status': 'invalid'}).status_code == 400
    assert client.get('/api/projects', headers=headers, params={'status': 'DONE'}).json()['data']['items'] == []


def test_latest_evaluation_matches_summary_without_answer_or_credentials(single_ready):
    path, _ = run_condition(single_ready, ConditionJudge())
    _, client, headers, service, *_ = single_ready
    summary = client.get(path + '/summary', headers=headers).json()['data']
    project = client.get(service.split('/services/')[0], headers=headers).json()['data']
    latest = project['latestEvaluation']
    assert latest['id'] == summary['evaluation']['id']
    assert latest['criteria'] == summary['criteria']
    assert latest['criteriaVersion'] == 2 and latest['mode'] == 'SINGLE'
    assert latest['provisional'] is False
    assert project['service']['connectionVerified'] is True
    assert project['service']['endpointHost'] == 'chatbot.example.com'
    listed = client.get('/api/projects', headers=headers, params={'status': 'DONE'}).json()['data']
    assert listed['statusCounts'] == {'SETUP': 0, 'RUNNING': 0, 'DONE': 1}
    assert listed['items'][0]['latestEvaluation'] == latest
    assert 'answer' not in str(latest) and 'secret_ciphertext' not in str(project)


def test_latest_connection_test_recovery_and_stale_revision(db_and_client):
    factory, client = db_and_client
    headers = register(client, 'connection_flow')
    project = client.post('/api/projects', headers=headers,
        json={'name': 'Flow', 'service': {'name': 'Bot'}}).json()['data']
    service = f"/api/projects/{project['id']}/services/{project['service']['id']}"
    assert client.get(service + '/connection-tests', headers=headers).json() == {'data': None}
    config = {**CONFIG, 'auth': {'type': 'NONE'}, 'executionMode': 'CONNECTION_ONLY', 'promptPointer': None}
    assert client.put(service + '/connection', headers=headers, json=config).status_code == 200
    response = client.post(service + '/connection-tests', headers={**headers, 'Idempotency-Key': 'test-1'},
        json={'connectionRevision': 1, 'question': 'Q'})
    assert response.status_code == 202
    latest = client.get(service + '/connection-tests', headers=headers).json()['data']
    assert latest['id'] == response.json()['data']['id'] and latest['status'] == 'QUEUED'
    assert latest['isCurrentRevision'] is True

    async def answer(*args):
        return 'OK'

    assert asyncio.run(process_one(factory, sender=answer))
    latest = client.get(service + '/connection-tests', headers=headers).json()['data']
    assert latest['status'] == 'SUCCEEDED' and latest['answer'] == 'OK'
    assert client.put(service + '/connection', headers=headers,
        json={**config, 'expectedRevision': 1, 'timeoutMs': 2000}).status_code == 200
    assert client.get(service + '/connection-tests', headers=headers).json()['data']['isCurrentRevision'] is False
    assert client.get(f"/api/projects/{project['id']}", headers=headers).json()['data']['service']['connectionVerified'] is False
    other = register(client, 'connection_other')
    assert client.get(service + '/connection-tests', headers=other).status_code == 404


def test_counts_ignore_confirmed_testsets_in_trash(db_and_client):
    from test_policy_testsets import setup
    from app.models.policy import TestSet, TestSetVersion
    factory, client = db_and_client
    headers, project, *_ = setup(client)
    with factory() as db:
        version = db.scalar(select(TestSetVersion))
        version.status = 'confirmed'
        db.get(TestSet, version.testset_id).deleted = True
        db.commit()
    data = client.get('/api/projects', headers=headers, params={'status': 'SETUP'}).json()['data']
    assert data['items'][0]['id'] == project.rsplit('/', 1)[1]
    assert data['items'][0]['progress']['status'] == 'SETUP'
