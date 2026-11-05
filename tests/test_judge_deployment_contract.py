"""Exercise deployed judge settings through API/worker contracts without network calls."""
import asyncio
from copy import deepcopy
import json
import os

from dotenv import dotenv_values
import pytest

from app import evaluation_worker as worker
from app.models.policy import TestSetVersion as SetVersion
from app.services import bedrock_runtime as runtime
from app.services.condition_evaluation import unavailable
from deploy.question_generation_env import DEFAULT_MODEL, DEFAULT_REGION, install
from test_bedrock_ai import tool_response
from test_condition_evaluation import ConditionJudge, answer
from test_evaluations import submit
from test_single_evaluation import single_ready  # noqa: F401


@pytest.mark.parametrize('verdict,result_status,pass_rate', [
    ('pass', 'MET', 100), ('fail', 'UNMET', 0), (None, 'REVIEW_REQUIRED', None),
])
def test_installed_judge_settings_reach_frontend_contract(single_ready, monkeypatch, tmp_path,
                                                         verdict, result_status, pass_rate):
    factory, client, headers, service, body, _ = single_ready
    target = tmp_path / 'runtime.env'
    target.write_text('EVALUATION_JUDGE_PROVIDER=\n')
    payload = {'region': DEFAULT_REGION, 'generationModel': DEFAULT_MODEL, 'judgeModel': DEFAULT_MODEL}
    assert install(payload, target, uid=os.getuid(), gid=os.getgid())
    for name, value in dotenv_values(target).items():
        monkeypatch.setenv(name, value)
    calls = []

    class OfflineClient:
        def converse(self, **request):
            calls.append(request)
            data = json.loads(request['messages'][0]['content'][0]['text'])
            if verdict is None:
                output = unavailable(data['question'], '자료만으로는 판단할 수 없음')
            else:
                output = asyncio.run(ConditionJudge().judge(**data))
                output['policy']['verdict'] = verdict
                for decision in output['conditionResults']:
                    decision['verdict'] = verdict
                    if verdict == 'fail':
                        decision['issue'] = {'kind': 'condition_omitted',
                            'missingGuidance': ['테스트용 누락 조건'], 'recommendation': '조건을 안내하세요.'}
            return tool_response(output, name='submit_evaluation')

    # Replace the entire transport before API submission or worker selection.
    monkeypatch.setattr(runtime, 'client_for', lambda *args: OfflineClient())
    path = submit(client, headers, service, body)
    queued = client.get(path, headers=headers).json()['data']
    assert queued['judge']['provider'] == 'bedrock'
    assert queued['judge']['model'] == DEFAULT_MODEL and queued['judge']['region'] == DEFAULT_REGION
    assert queued['judge']['adapter'] != 'unconfigured'
    asyncio.run(worker.process_one(factory, sender=answer))
    assert len(calls) == 1 and calls[0]['modelId'] == DEFAULT_MODEL
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    detail = client.get(path + '/items/' + items[0]['id'], headers=headers).json()['data']
    assert detail['resultStatus'] == result_status
    assert detail['a']['judgeError'] is None
    assert detail['a']['criteria']['policy']['verdict'] == verdict
    summary = client.get(path + '/summary', headers=headers).json()['data']
    policy = summary['criteria']['policy']['a']
    assert policy['passRate'] == pass_rate
    assert policy['denominator'] == (0 if verdict is None else 1)
    assert client.get(path + '/items', headers=headers, params={'resultStatus': result_status}).json()['data']['items'] == items


@pytest.mark.parametrize('mode', ['SINGLE', 'COMPARE'])
@pytest.mark.parametrize('count,expected_status', [(200, 202), (201, 409)])
def test_evaluation_question_limit_is_enforced_before_queueing(single_ready, mode, count, expected_status):
    factory, client, headers, service, body, comparison = single_ready
    with factory() as db:
        version = db.get(SetVersion, body['testSetVersionId'])
        snapshot = deepcopy(version.snapshot)
        original = snapshot['questions'][0]
        snapshot['questions'] = [{**deepcopy(original), 'id': f'q-{index}'} for index in range(count)]
        version.snapshot = snapshot
        db.commit()
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'bounded'},
                           json={**(body if mode == 'SINGLE' else comparison), 'mode': mode})
    assert response.status_code == expected_status, response.text
    if expected_status == 202:
        assert response.json()['data']['totalQuestions'] == 200
    else:
        assert response.json()['error']['code'] == 'INVALID_TESTSET'
        assert client.get(service + '/evaluations', headers=headers).json()['data']['items'] == []
