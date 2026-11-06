"""The deployed item pool must respect the Bedrock SDK's single-call budget."""
import asyncio
from threading import Event

import pytest

from app import evaluation_worker as worker
from app.main import app
from app.services import bedrock_runtime as runtime
from app.services.bedrock_ai import BedrockEvaluationJudge
from app.services.evaluation_judge import get_evaluation_judge
from test_condition_evaluation import ConditionJudge
from test_evaluations import prepare, submit


@pytest.mark.parametrize('mode', ['SINGLE', 'COMPARE'])
def test_deployed_pool_grades_all_items_without_competing_for_bedrock(db_and_client, monkeypatch, mode):
    factory, client = db_and_client
    headers, service, original, _ = prepare(factory, client, monkeypatch, question_count=3)
    body = ({key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
            if mode == 'SINGLE' else {**original, 'mode': 'COMPARE'})
    monkeypatch.setenv('EVALUATION_ITEM_CONCURRENCY', '3')
    runner = runtime._BoundedJudgeCalls()
    monkeypatch.setattr(runtime, '_judge_calls', runner)
    started, release = Event(), Event()
    sdk_calls, company_calls = [], []

    def slow_sdk(**kwargs):
        sdk_calls.append(kwargs)
        started.set()
        assert release.wait(timeout=5), 'test must release the blocked SDK call'
        return asyncio.run(ConditionJudge().judge(**kwargs['payload']))

    async def sender(*args):
        company_calls.append(True)
        return '출발 24시간 전까지 취소 가능합니다.'

    monkeypatch.setattr(runtime, 'converse_json', slow_sdk)
    judge = BedrockEvaluationJudge(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION)
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)

    async def scenario():
        task = asyncio.create_task(worker.process_one(factory, sender=sender, judge=judge))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # Other items stay unstarted while the first SDK call owns the single slot.
            assert len(company_calls) == len(sdk_calls) == 1
        finally:
            release.set()
            await task

    try:
        asyncio.run(scenario())
        expected_calls = 3 if mode == 'SINGLE' else 6
        assert len(company_calls) == len(sdk_calls) == expected_calls
        items = client.get(path + '/items', headers=headers).json()['data']['items']
        assert len(items) == 3
        for item in items:
            detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
            for name in ('a',) if mode == 'SINGLE' else ('a', 'b'):
                side = detail[name]
                assert side['callStatus'] == 'SUCCEEDED'
                assert side['judgeError'] is None
                assert all(result['status'] == 'judged' for result in side['criteria'].values())
                assert all(result['status'] == 'judged' for result in side['conditionResults'])
        summary = client.get(path + '/summary', headers=headers).json()['data']
        assert summary['evaluation']['completedQuestions'] == 3
        for name in ('a',) if mode == 'SINGLE' else ('a', 'b'):
            for criterion in summary['criteria'].values():
                assert criterion[name]['judgeFailed'] == 0
                assert criterion[name]['denominator'] == 3
    finally:
        release.set()
        runner._executor.shutdown(wait=True)
