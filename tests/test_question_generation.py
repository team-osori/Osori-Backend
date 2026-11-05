import asyncio
import json

import aiohttp
import pytest

from app.core.errors import ApiError
from app.services.question_generation import OpenAIQuestionGenerator, generation_schema, get_question_generator, UnconfiguredGenerator


def candidate(category='normal'):
    return {'question': '취소할 수 있나요? ' + category, 'expectedBehavior': '취소 기한을 안내한다.',
        'forbiddenBehaviors': [], 'policyVersionId': 'policy-v1', 'ruleKeys': ['refund'],
        'evidenceQuote': '24시간 전까지 취소 가능', 'category': category,
        'expectedConditions': [{'description': '취소 기한을 안내한다.', 'kind': 'required', 'category': 'policy',
            'policyVersionId': 'policy-v1', 'ruleKeys': ['refund'], 'evidenceQuote': '24시간 전까지 취소 가능'}]}


def completed(questions):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps({'questions': questions})}]}]}


def test_responses_request_and_structured_batch(monkeypatch):
    generator = OpenAIQuestionGenerator('test-secret')

    async def request(body):
        assert body['store'] is False and 'test-secret' not in json.dumps(body)
        assert body['text']['format']['strict'] is True
        payload = json.loads(body['input'][0]['content'])
        assert payload['categories'] == ['normal', 'exception', 'boundary']
        assert body['instructions'] and payload['policies'][0]['content'] == '정책 데이터'
        return completed([candidate(c) for c in payload['categories']])

    monkeypatch.setattr(generator, '_request', request)
    result = generator.generate(policies=[{'content': '정책 데이터'}], count=3)
    assert len(result.questions) == 3 and result.questions[2].category == 'boundary'
    assert all(c.id is None for q in result.questions for c in q.expectedConditions)
    schema = generation_schema()
    assert 'id' not in schema['$defs']['ExpectedCondition']['properties']


@pytest.mark.parametrize('result,code', [
    ({'status': 'incomplete'}, 'GENERATION_INCOMPLETE'),
    ({'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'private-data'}]}]}, 'GENERATION_REFUSED'),
    (completed([candidate('exception')]), 'INVALID_GENERATION_OUTPUT'),
    (completed([candidate(), candidate()]), 'INVALID_GENERATION_OUTPUT'),
    ({'status': 'completed', 'output': []}, 'INVALID_GENERATION_OUTPUT'),
])
def test_invalid_or_refused_results_are_not_saved(monkeypatch, result, code):
    generator = OpenAIQuestionGenerator('private-data')

    async def request(_):
        return result

    monkeypatch.setattr(generator, '_request', request)
    with pytest.raises(ApiError) as caught:
        generator.generate(policies=[], count=1)
    assert caught.value.code == code and 'private-data' not in str(caught.value)


@pytest.mark.parametrize('error,code', [(TimeoutError('secret'), 'GENERATION_TIMEOUT'),
    (aiohttp.ClientConnectionError('secret'), 'GENERATION_UNAVAILABLE')])
def test_transport_errors_are_sanitized(monkeypatch, error, code):
    generator = OpenAIQuestionGenerator('secret')

    async def request(_):
        raise error

    monkeypatch.setattr(generator, '_request', request)
    with pytest.raises(ApiError) as caught:
        generator.generate(policies=[], count=1)
    assert caught.value.code == code and 'secret' not in str(caught.value)


def test_provider_must_be_enabled_and_input_is_bounded(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('QUESTION_GENERATION_API_KEY', '')
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', '')
    assert isinstance(get_question_generator(), UnconfiguredGenerator)
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'openai')
    generator = get_question_generator()
    assert isinstance(generator, OpenAIQuestionGenerator)
    with pytest.raises(ApiError) as caught:
        generator.generate(policies=[{'content': '한' * 200000}], count=1)
    assert caught.value.code == 'GENERATION_INPUT_TOO_LARGE'


@pytest.mark.parametrize('status,code', [(401, 'GENERATION_AUTH_FAILED'), (403, 'GENERATION_AUTH_FAILED'),
    (429, 'GENERATION_RATE_LIMITED'), (302, 'GENERATION_UNAVAILABLE'), (500, 'GENERATION_UNAVAILABLE')])
def test_http_errors_do_not_read_or_expose_provider_body(monkeypatch, status, code):
    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    response = Response()
    response.status = status

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['timeout'].total == 60
        def post(self, url, **kwargs):
            assert url == 'https://api.openai.com/v1/responses'
            assert kwargs['allow_redirects'] is False
            return response

    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    with pytest.raises(ApiError) as caught:
        asyncio.run(OpenAIQuestionGenerator('secret')._request({}))
    assert caught.value.code == code
