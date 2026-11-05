"""Cancelled waiters cannot free a still-running paid SDK call or grow executor queues."""
import asyncio
from threading import Event

import pytest

from app import evaluation_worker as worker
from app.main import app
from app.services import bedrock_runtime as runtime
from app.services.bedrock_ai import BedrockEvaluationJudge
from app.services.evaluation_judge import get_evaluation_judge, JudgeError
from test_condition_evaluation import ConditionJudge, answer
from test_evaluations import submit
from test_single_evaluation import single_ready  # noqa: F401


def test_worker_timeout_bounds_lingering_calls_and_preserves_saved_verdict(single_ready, monkeypatch):
    factory, client, headers, service, body, _ = single_ready
    runner = runtime._BoundedJudgeCalls()
    monkeypatch.setattr(runtime, '_judge_calls', runner)
    started, release = Event(), Event()
    calls = []

    def slow_sdk(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=5), 'test must release the blocked SDK call'
        return asyncio.run(ConditionJudge().judge(**kwargs['payload']))

    monkeypatch.setattr(runtime, 'converse_json', slow_sdk)
    monkeypatch.setattr(BedrockEvaluationJudge, 'timeout_seconds', 0.2)
    judge = BedrockEvaluationJudge(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION)
    app.dependency_overrides[get_evaluation_judge] = lambda: judge

    def detail(path):
        item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
        return client.get(path + '/items/' + item['id'], headers=headers).json()['data']

    try:
        path = submit(client, headers, service, body)
        assert asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
        assert started.is_set() and not release.is_set()
        timed_out = detail(path)
        assert timed_out['a']['callStatus'] == 'SUCCEEDED'
        assert timed_out['a']['judgeError']['code'] == 'JUDGE_TIMEOUT'

        other = BedrockEvaluationJudge(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION)
        async def repeat_cancelled_waiters():
            for _ in range(10):
                with pytest.raises(JudgeError) as caught:
                    await other.judge(**calls[0]['payload'])
                assert caught.value.code == 'JUDGE_BUSY'
            # A stranded SDK call must not monopolize asyncio.run's default executor.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 'available'), timeout=1) == 'available'
        asyncio.run(repeat_cancelled_waiters())
        second = submit(client, headers, service, body, key='bedrock-occupied')
        asyncio.run(worker.process_one(factory, sender=answer, judge=other))
        assert detail(second)['a']['judgeError']['code'] == 'JUDGE_BUSY'
        assert len(calls) == 1
        counts = client.get(second + '/summary', headers=headers).json()['data']['criteria']['policy']['a']
        assert counts['judgeFailed'] == 1 and counts['callFailed'] == 0
        release.set()
        # A barrier in the dedicated executor observes the SDK completion and slot release.
        runner._executor.submit(lambda: None).result(timeout=2)
        assert detail(path)['a'] == timed_out['a'], 'late SDK success cannot overwrite JUDGE_TIMEOUT'
        recovered = submit(client, headers, service, body, key='bedrock-recovered')
        asyncio.run(worker.process_one(factory, sender=answer, judge=other))
        assert detail(recovered)['resultStatus'] == 'MET'
        assert len(calls) == 2
    finally:
        release.set()
        runner._executor.shutdown(wait=True)


def test_cancel_before_sdk_start_releases_capacity_without_executing_the_call():
    runner = runtime._BoundedJudgeCalls()
    release = Event()
    calls = []
    blocker = runner._executor.submit(release.wait, 5)
    async def scenario():
        task = asyncio.create_task(runner.run(lambda: calls.append('must-not-run')))
        await asyncio.sleep(0)  # The guarded future is queued behind the test blocker.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # Propagate cancellation to the concurrent future.
        release.set()
        await asyncio.wrap_future(blocker)
        assert await runner.run(lambda: 'recovered') == 'recovered'
        assert calls == []
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        runner._executor.shutdown(wait=True, cancel_futures=True)


def test_submission_failure_releases_capacity(monkeypatch):
    runner = runtime._BoundedJudgeCalls()
    def fail(*args, **kwargs):
        raise RuntimeError('private submission details')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runner._executor, 'submit', fail)
            with pytest.raises(runtime.BedrockError) as caught:
                asyncio.run(runner.run(lambda: 'not submitted'))
            assert caught.value.code == 'UNAVAILABLE'
            assert 'private' not in str(caught.value)
        assert asyncio.run(runner.run(lambda: 'recovered')) == 'recovered'
    finally:
        runner._executor.shutdown(wait=True)
