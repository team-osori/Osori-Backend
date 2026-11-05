import asyncio

from app import evaluation_worker as worker
from app.main import app
from app.services.evaluation_judge import get_evaluation_judge
from test_condition_evaluation import ConditionJudge, answer
from test_evaluations import prepare, submit
from test_single_evaluation import single_ready  # noqa: F401


def test_cancel_queued_and_resume_without_duplicate_calls(single_ready):
    factory, client, headers, service, body, _ = single_ready
    judge = ConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)
    cancelled = client.post(path + '/cancel', headers=headers, json={})
    assert cancelled.status_code == 200 and cancelled.json()['data']['status'] == 'CANCELLED'
    assert cancelled.json()['data']['completedQuestions'] == 0
    assert client.post(path + '/cancel', headers=headers, json={}).status_code == 200
    assert asyncio.run(worker.process_one(factory, sender=answer, judge=judge)) is False
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': 999}).status_code == 409
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': body['connectionRevision']}).status_code == 202
    calls = []
    async def sender(*args):
        calls.append(1)
        return await answer(*args)
    assert asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    assert len(calls) == 1
    assert client.get(path, headers=headers).json()['data']['status'] == 'COMPLETED'
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': body['connectionRevision']}).status_code == 409
    assert client.post(path + '/cancel', headers=headers, json={}).status_code == 409


def exercise_cancel_running(factory, client, headers, service, original):
    judge = ConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    path = submit(client, headers, service, body)
    calls = []
    async def scenario():
        entered = asyncio.Event()
        async def sender(*args):
            calls.append(1)
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(worker.process_one(factory, sender=sender, judge=judge))
        await asyncio.wait_for(entered.wait(), 5)
        response = client.post(path + '/cancel', headers=headers, json={})
        assert response.status_code == 200, response.text
        await asyncio.wait_for(task, 5)
    asyncio.run(scenario())
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    assert sum(item['aCallStatus'] == 'INTERRUPTED' for item in items) == 1
    assert sum(item['aCallStatus'] == 'NOT_RUN' for item in items) == 1
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': body['connectionRevision']}).status_code == 202
    async def resumed(*args):
        calls.append(1)
        return await answer(*args)
    asyncio.run(worker.process_one(factory, sender=resumed, judge=judge))
    assert len(calls) == 2  # One uncertain call is never sent again.
    assert client.get(path, headers=headers).json()['data']['status'] == 'PARTIAL_FAILED'
    return path


def test_cancel_running_fences_old_worker_and_resume_skips_uncertain_calls(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, original, _ = prepare(factory, client, monkeypatch, question_count=2)
    exercise_cancel_running(factory, client, headers, service, original)


def test_analysis_capabilities_are_explicit(single_ready):
    _, client, headers, service, *_ = single_ready
    response = client.get(service + '/evaluations/capabilities', headers=headers)
    assert response.status_code == 200, response.text
    features = response.json()['data']
    assert features['causeHypotheses']['supported'] is True
    assert all(not features[key]['supported'] and features[key]['reason'] for key in features if key != 'causeHypotheses')


def test_resume_compare_preserves_saved_side_and_does_not_replay_uncertain_side(db_and_client, monkeypatch):
    from test_evaluations import TestJudge
    factory, client = db_and_client
    headers, service, body, _ = prepare(factory, client, monkeypatch, question_count=2)
    judge = TestJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)
    calls = []
    async def scenario():
        entered = asyncio.Event()
        async def sender(*args):
            calls.append(1)
            if len(calls) == 2:
                entered.set()
                await asyncio.Event().wait()
            return await answer(*args)
        task = asyncio.create_task(worker.process_one(factory, sender=sender, judge=judge))
        await asyncio.wait_for(entered.wait(), 5)
        assert client.post(path + '/cancel', headers=headers, json={}).status_code == 200
        await asyncio.wait_for(task, 5)
    asyncio.run(scenario())
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    saved = next(item for item in items if item['aCallStatus'] == 'SUCCEEDED')
    assert saved['bCallStatus'] == 'INTERRUPTED'
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': body['connectionRevision']}).status_code == 202
    async def resumed(*args):
        calls.append(1)
        return await answer(*args)
    asyncio.run(worker.process_one(factory, sender=resumed, judge=judge))
    assert len(calls) == 4  # Only A/B of the second question are sent after resuming.
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    assert next(item for item in items if item['id'] == saved['id']) == saved
    assert client.get(path, headers=headers).json()['data']['status'] == 'PARTIAL_FAILED'
