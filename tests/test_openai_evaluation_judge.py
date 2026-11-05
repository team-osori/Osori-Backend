import asyncio
import json

import aiohttp
import pytest

from app import evaluation_worker as worker
from app.services.evaluation_judge import get_evaluation_judge, UnconfiguredJudge
from app.services.openai_evaluation_judge import OpenAIEvaluationJudge, JudgeError, judgment_schema
from test_condition_evaluation import ConditionJudge, answer
from test_evaluations import submit
from test_single_evaluation import single_ready  # noqa: F401


def completed(output):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps(output)}]}]}


def test_responses_adapter_runs_through_normal_worker_with_a_conditions(single_ready, monkeypatch):
    factory, client, headers, service, body, original = single_ready
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', 'openai')
    monkeypatch.setenv('EVALUATION_JUDGE_API_KEY', '')
    monkeypatch.setenv('QUESTION_GENERATION_API_KEY', 'test-only-evaluation-key')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', 'gpt-4.1-mini-2025-04-14')
    calls = []

    async def request(self, payload):
        assert payload['model'] == 'gpt-4.1-mini-2025-04-14'
        assert payload['store'] is False and payload['text']['format']['strict'] is True
        assert 'test-only-evaluation-key' not in json.dumps(payload)
        assert 'kind=forbidden' in payload['instructions']
        data = json.loads(payload['input'][0]['content'])
        assert {condition['kind'] for condition in data['question']['expectedConditions']} == {'required', 'forbidden'}
        calls.append(data)
        return completed(await ConditionJudge().judge(**data))

    monkeypatch.setattr(OpenAIEvaluationJudge, '_request', request)
    path = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer))
    assert len(calls) == 1
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
    assert detail['a']['judgeError'] is None
    assert all(result['verdict'] == 'pass' for result in detail['a']['conditionResults'])
    assert detail['resultStatus'] == 'MET'


@pytest.mark.parametrize('envelope,code', [
    ({'status': 'incomplete'}, 'JUDGE_INCOMPLETE'),
    ({'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'secret-echo'}]}]}, 'JUDGE_REFUSED'),
    ({'status': 'completed', 'output': []}, 'INVALID_JUDGE_OUTPUT'),
    (completed({'bad': 'secret-echo'}), 'INVALID_JUDGE_OUTPUT'),
])
def test_provider_failures_stay_judge_failures_and_do_not_leak(single_ready, monkeypatch, envelope, code):
    factory, client, headers, service, body, original = single_ready
    from app.main import app
    judge = OpenAIEvaluationJudge('secret-echo', 'test-model')
    app.dependency_overrides[get_evaluation_judge] = lambda: judge

    async def request(_):
        return envelope

    monkeypatch.setattr(judge, '_request', request)
    path = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    response = client.get(path + '/items/' + item['id'], headers=headers)
    result = response.json()['data']['a']
    assert result['callStatus'] == 'SUCCEEDED' and result['judgeError']['code'] == code
    assert all(c['status'] == 'error' for c in result['conditionResults'])
    assert 'secret-echo' not in response.text
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['criteria']['policy']['a']['judgeFailed'] == 1
    assert summary['criteria']['policy']['a']['callFailed'] == 0


@pytest.mark.parametrize('status,code', [(401, 'JUDGE_AUTH_FAILED'), (403, 'JUDGE_AUTH_FAILED'),
    (429, 'JUDGE_RATE_LIMITED'), (302, 'JUDGE_UNAVAILABLE'), (500, 'JUDGE_UNAVAILABLE')])
def test_transport_is_fixed_and_provider_errors_are_sanitized(monkeypatch, status, code):
    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    response = Response()
    response.status = status

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['timeout'].total == 25
        def post(self, url, **kwargs):
            assert url == 'https://api.openai.com/v1/responses' and kwargs['allow_redirects'] is False
            return response
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    with pytest.raises(JudgeError) as error:
        asyncio.run(OpenAIEvaluationJudge('private-key', 'test-model')._request({}))
    assert error.value.code == code and 'private-key' not in str(error.value)


def test_explicit_enable_key_precedence_input_bound_and_strict_schema(monkeypatch):
    for name in ('EVALUATION_JUDGE_API_KEY', 'QUESTION_GENERATION_API_KEY', 'OPENAI_API_KEY'):
        monkeypatch.setenv(name, '')
    monkeypatch.setenv('QUESTION_GENERATION_API_KEY', 'shared-test-key')
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', '')
    assert isinstance(get_evaluation_judge(), UnconfiguredJudge)
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', 'openai')
    judge = get_evaluation_judge()
    assert isinstance(judge, OpenAIEvaluationJudge)
    monkeypatch.setenv('EVALUATION_JUDGE_API_KEY', 'dedicated-test-key')
    assert get_evaluation_judge()._api_key == 'dedicated-test-key'
    with pytest.raises(JudgeError) as error:
        asyncio.run(judge.judge(question={'id': 'q'}, policies=['한' * 200000]))
    assert error.value.code == 'JUDGE_INPUT_TOO_LARGE'

    def check(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node['additionalProperties'] is False
                assert set(node['required']) == set(node['properties'])
            assert 'default' not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)
    for version in (1, 2):
        check(judgment_schema(version))


@pytest.mark.parametrize('failure,code', [(None, None), ('size', 'JUDGE_RESPONSE_TOO_LARGE'),
    ('key', 'INVALID_JUDGE_OUTPUT'), ('json', 'INVALID_JUDGE_OUTPUT'),
    ('timeout', 'JUDGE_TIMEOUT'), ('connection', 'JUDGE_UNAVAILABLE')])
def test_streamed_http_response_is_bounded_and_checked_before_storage(monkeypatch, failure, code):
    from app.services import openai_evaluation_judge as adapter
    result = {key: {'question_id': 'q', 'status': 'not_evaluable', 'verdict': None,
        'reason': '근거 부족', 'evidence_refs': [], 'error': None}
        for key in ('policy', 'response', 'retrieval', 'grounding')}
    raw = json.dumps(completed(result)).encode()
    if failure == 'size':
        monkeypatch.setattr(adapter, 'MAX_RESPONSE_BYTES', 10)
    elif failure == 'key':
        raw = json.dumps(completed({'reason': 'private-test-key'})).encode()
    elif failure == 'json':
        raw = b'not json'

    class Content:
        async def iter_chunked(self, size):
            yield raw[:10]
            yield raw[10:]

    class Response:
        status = 200
        content = Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class Session(Response):
        def __init__(self, **kwargs):
            pass
        def post(self, *args, **kwargs):
            if failure == 'timeout':
                raise TimeoutError()
            if failure == 'connection':
                raise aiohttp.ClientConnectionError()
            return Response()

    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    judge = OpenAIEvaluationJudge('private-test-key', 'test-model')
    if code:
        with pytest.raises(JudgeError) as error:
            asyncio.run(judge.judge(question={'id': 'q'}, criteria={'version': 1}))
        assert error.value.code == code and 'private-test-key' not in str(error.value)
    else:
        output = asyncio.run(judge.judge(question={'id': 'q'}, criteria={'version': 1}))
        assert output.model_dump() == result
