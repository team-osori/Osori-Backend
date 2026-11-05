import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from threading import Barrier

from sqlalchemy import select

from app import evaluation_worker as worker
from app.models.evaluation import Evaluation, EvaluationItem
from test_evaluations import prepare, submit
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
