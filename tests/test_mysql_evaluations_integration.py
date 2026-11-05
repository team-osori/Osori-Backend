import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from threading import Barrier

import pytest
from sqlalchemy import select

from app import evaluation_worker as worker
from app.models.evaluation import Evaluation, EvaluationItem
from test_evaluations import exercise_interrupted_run, prepare, submit
from test_mysql_auth_integration import mysql_client  # noqa: F401 - shared fixture


def test_mysql_duplicate_submission_and_two_worker_claims(mysql_client, monkeypatch):
    """Verify MySQL serializes duplicate submissions and prevents duplicate A/B calls."""
    client, factory, _, _ = mysql_client
    headers, service, body, _ = prepare(factory, client, monkeypatch)
    barrier = Barrier(2)

    def create():
        """Race two equal idempotency keys against the real MySQL unique constraint."""
        barrier.wait(timeout=10)
        return client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'same'}, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: create(), range(2)))
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].json()['data']['id'] == responses[1].json()['data']['id']
    path = responses[0].headers['location']
    start = Barrier(2)
    calls = []

    async def sender(config, payload, outgoing):
        """Simulate the company transport without opening a network connection."""
        calls.append(payload['messages'][0]['content'])
        await asyncio.sleep(0.05)
        return '기업 답변'

    def execute():
        """Start a worker concurrently to detect duplicate company API calls."""
        start.wait(timeout=10)
        return asyncio.run(worker.process_one(factory, sender=sender))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: execute(), range(2)))
    assert sorted(calls) == ['A prompt', 'B prompt']
    result = client.get(path, headers=headers).json()['data']
    assert result['status'] == 'COMPLETED' and result['completedQuestions'] == 1
    with factory() as db:
        row = db.get(Evaluation, result['id'])
        assert row.secret_ciphertext is None
        assert len(db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == row.id)).all()) == 1


def test_mysql_queue_survives_fresh_worker_process(mysql_client, monkeypatch):
    """Process persisted work and decrypt submitted credentials in a fresh process."""
    client, factory, env, root = mysql_client
    headers, service, body, _ = prepare(factory, client, monkeypatch)
    path = submit(client, headers, service, body)
    script = '''
import asyncio, json
from app.evaluation_worker import process_one
async def sender(config, payload, headers):
    assert headers['Authorization'] == 'Bearer test-only-secret-token'
    return '재시작 이후 기업 답변'
print(json.dumps({'worked': asyncio.run(process_one(sender=sender))}))
'''
    child_env = {**env, 'CONNECTION_ENCRYPTION_KEY': os.environ['CONNECTION_ENCRYPTION_KEY']}
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=child_env,
                            text=True, capture_output=True, check=True, timeout=30)
    assert json.loads(result.stdout) == {'worked': True}
    assert client.get(path, headers=headers).json()['data']['status'] == 'COMPLETED'
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    assert client.get(path + '/items/' + item['id'], headers=headers).json()['data']['b']['answer'] == '재시작 이후 기업 답변'


@pytest.mark.parametrize('phase', ['a', 'b'])
def test_mysql_crash_preserves_call_intent_and_unstarted_states(mysql_client, monkeypatch, phase):
    """Round-trip call intent and recovery states through real MySQL JSON columns."""
    client, factory, _, _ = mysql_client
    headers, service, body, _ = prepare(factory, client, monkeypatch, question_count=2)
    exercise_interrupted_run(factory, client, headers, service, body, phase)


def test_mysql_single_evaluation_and_concurrent_human_reviews(mysql_client, monkeypatch):
    """Only one reviewer can claim a revision; the AI JSON and frozen question remain intact."""
    from app.main import app
    from app.models.evaluation import EvaluationReview
    from app.services.evaluation_judge import get_evaluation_judge
    from test_evaluation_analysis import FailingConditionJudge
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch)
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    judge = FailingConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)
    calls = []

    async def sender(*args):
        calls.append(1)
        return '기업 답변'

    asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    assert len(calls) == 1
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    endpoint = path + '/items/' + item['id']
    before = client.get(endpoint, headers=headers).json()['data']
    barrier = Barrier(2)

    def review(_):
        barrier.wait(timeout=10)
        return client.post(endpoint + '/reviews', headers=headers, json={
            'expectedRevision': 0, 'conditionId': before['input']['expectedConditions'][0]['id'],
            'verdict': 'pass', 'reason': '담당자가 원문 확인'})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(review, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    after = client.get(endpoint, headers=headers).json()['data']
    assert after['a'] == before['a'] and after['input'] == before['input']
    assert after['resultStatus'] == 'MET' and after['reviewRevision'] == 1
    with factory() as db:
        assert len(db.scalars(select(EvaluationReview).where(EvaluationReview.item_id == item['id'])).all()) == 1


@pytest.mark.parametrize('provider', ['openai', 'bedrock'])
def test_mysql_a_generation_through_b_analysis_and_rerun(mysql_client, monkeypatch, provider):
    """Round-trip A's real conditions/history and B's judgments/comparison through MySQL."""
    from test_a_b_workflow import test_generated_a_conditions_flow_through_b_analysis_and_frozen_rerun
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch)
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    ready = (factory, client, headers, service, body, original)
    test_generated_a_conditions_flow_through_b_analysis_and_frozen_rerun(ready, monkeypatch, provider)


def test_mysql_project_progress_and_review_counts(mysql_client, monkeypatch):
    """Exercise window functions, JSON paths and latest human decisions on MySQL."""
    from app.main import app
    from app.services.evaluation_judge import get_evaluation_judge
    from test_condition_evaluation import ConditionJudge, answer
    from test_project_progress import exercise_finished_progress
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch)
    judge = ConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    path = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
    exercise_finished_progress(factory, client, headers, service, path, detail)


def test_mysql_cancel_resume_preserves_uncertain_calls(mysql_client, monkeypatch):
    from test_evaluation_control import exercise_cancel_running
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch, question_count=2)
    exercise_cancel_running(factory, client, headers, service, original)


def test_mysql_answer_and_condition_reviews_share_revision(mysql_client, monkeypatch):
    """O/X and condition writes race on the same item CAS, across separate history tables."""
    from app.models.evaluation import EvaluationAnswerReview, EvaluationReview
    from test_condition_evaluation import ConditionJudge, run_condition
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch)
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    path, detail = run_condition((factory, client, headers, service, body, original), ConditionJudge())
    endpoint = path + '/items/' + detail['id']
    barrier = Barrier(2)

    def save(scope):
        payload = {'expectedRevision': 0, 'verdict': 'pass', 'reason': '정책과 답변 확인'}
        if scope == 'reviews':
            payload['conditionId'] = detail['input']['expectedConditions'][0]['id']
        barrier.wait(timeout=10)
        return client.post(endpoint + '/' + scope, headers=headers, json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ['answer-reviews', 'reviews']))
    assert sorted(response.status_code for response in responses) == [201, 409]
    after = client.get(endpoint, headers=headers).json()['data']
    assert after['reviewRevision'] == 1 and after['a'] == detail['a']
    with factory() as db:
        conditions = db.scalars(select(EvaluationReview).where(EvaluationReview.item_id == detail['id'])).all()
        answers = db.scalars(select(EvaluationAnswerReview).where(EvaluationAnswerReview.item_id == detail['id'])).all()
        assert len(conditions) + len(answers) == 1
    # The Figma buttons do not require a fabricated reason; NULL round-trips on MySQL.
    saved = client.post(endpoint + '/answer-reviews', headers=headers,
        json={'expectedRevision': 1, 'verdict': 'pass'})
    assert saved.status_code == 201 and saved.json()['data']['reason'] is None
    board = client.get(path + '/grading', headers=headers)
    assert board.status_code == 200, board.text
    assert board.json()['data']['items'][0]['questionNumber'] == 1
    assert board.json()['data']['items'][0]['answerReview']['reason'] is None
    summary = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']
    assert board.json()['data']['counts'] == summary['screenCounts']
