import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import evaluation_worker as worker
from app.models.connection import Connection
from app.models.evaluation import Evaluation, EvaluationItem
from app.services.auth import now
from test_evaluations import prepare, submit


@pytest.fixture
def single_ready(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, original, path = prepare(factory, client, monkeypatch)
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    return factory, client, headers, service, body, original


def test_single_calls_once_without_prompt_registration_requirement(single_ready):
    factory, client, headers, service, body, original = single_ready
    path = submit(client, headers, service, body)
    assert client.get(path, headers=headers).json()['data']['mode'] == 'SINGLE'
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    assert items[0]['bCallStatus'] == 'NOT_APPLICABLE' and items[0]['changes'] == {}
    detail = client.get(path + '/items/' + items[0]['id'], headers=headers)
    assert detail.status_code == 200 and detail.json()['data']['b'] is None
    calls = []

    async def sender(config, payload, outgoing):
        calls.append(payload)
        assert payload['messages'][0]['content'] == config['requestTemplate']['messages'][0]['content']
        return '출발 24시간 전까지 취소할 수 있습니다.'

    asyncio.run(worker.process_one(factory, sender=sender))
    assert len(calls) == 1
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['evaluation']['status'] == 'COMPLETED'
    assert summary['evaluation']['failedQuestions'] == 0
    assert summary['criteria']['policy']['b'] is None
    assert summary['criteria']['policy']['comparison'] is None
    assert client.get(path + '/inputs', headers=headers).json()['data']['useConfiguredPrompt'] is True


def test_optional_single_prompt_and_connection_only_validation(single_ready):
    factory, client, headers, service, body, original = single_ready
    path = submit(client, headers, service, {**body, 'promptVersionId': original['aPromptVersionId']})
    calls = []

    async def sender(config, payload, outgoing):
        calls.append(payload)
        return '답변'

    asyncio.run(worker.process_one(factory, sender=sender))
    assert len(calls) == 1 and calls[0]['messages'][0]['content'] == 'A prompt'
    with factory() as db:
        connection = db.scalar(select(Connection))
        connection.config = {**connection.config, 'executionMode': 'CONNECTION_ONLY'}
        db.commit()
    assert submit(client, headers, service, body, key='connection-only')
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'bad'},
                           json={**body, 'promptVersionId': original['aPromptVersionId']})
    assert response.status_code == 409
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'mixed'},
                           json={**original, 'mode': 'SINGLE'})
    assert response.status_code == 400


def test_single_abandoned_worker_does_not_create_or_count_a_second_call(single_ready):
    factory, client, headers, service, body, original = single_ready
    path = submit(client, headers, service, body)
    with factory() as db:
        run = db.scalar(select(Evaluation))
        run.status = 'RUNNING'
        run.heartbeat_at = now() - timedelta(seconds=worker.LEASE_SECONDS + 1)
        item = db.scalar(select(EvaluationItem))
        item.status = 'RUNNING'
        item.a = worker.pending_side('RUNNING', item.question_id)
        db.commit()
    worker.expire_abandoned(factory)
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['evaluation']['status'] == 'FAILED'
    assert summary['criteria']['policy']['a']['callInterrupted'] == 1
    assert summary['criteria']['policy']['b'] is None
    with factory() as db:
        assert db.scalar(select(EvaluationItem)).b is None


@pytest.mark.parametrize('change', ['IMPROVED', 'WORSENED', 'UNCHANGED_PASS', 'UNCHANGED_FAIL', 'NOT_COMPARABLE'])
def test_single_rejects_comparison_filters_instead_of_matching_unrun_b(single_ready, change):
    """A nonexistent B result must not appear as an incomparable paired result."""
    factory, client, headers, service, body, original = single_ready
    path = submit(client, headers, service, body)
    response = client.get(path + '/items', headers=headers, params={'criterion': 'policy', 'change': change})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'COMPARISON_REQUIRED'
    unfiltered = client.get(path + '/items', headers=headers).json()['data']['items']
    assert len(unfiltered) == 1 and unfiltered[0]['changes'] == {}
