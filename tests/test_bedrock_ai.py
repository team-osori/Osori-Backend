"""Validate the real boto3 contract offline; no AWS credentials or paid calls are used."""
import asyncio
import json

import boto3
from botocore.exceptions import NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber
import pytest

from app import evaluation_worker as worker
from app.core.errors import ApiError
from app.services import bedrock_runtime as runtime
from app.services.bedrock_ai import BedrockEvaluationJudge, BedrockQuestionGenerator
from app.services.evaluation_judge import get_evaluation_judge, JudgeError
from app.services.question_generation import get_question_generator
from test_condition_evaluation import ConditionJudge, answer
from test_evaluations import submit
from test_question_generation import candidate
from test_single_evaluation import single_ready  # noqa: F401


def tool_response(data, name='submit_questions', reason='tool_use'):
    return {'stopReason': reason, 'output': {'message': {'role': 'assistant', 'content': [
        {'toolUse': {'toolUseId': 'test-tool-id', 'name': name, 'input': data}}]}},
        'usage': {'inputTokens': 100, 'outputTokens': 100, 'totalTokens': 200}, 'metrics': {'latencyMs': 10}}


@pytest.fixture
def stubbed(monkeypatch):
    client = boto3.Session().client('bedrock-runtime', region_name=runtime.DEFAULT_REGION,
                                   aws_access_key_id='offline-test', aws_secret_access_key='offline-test')
    monkeypatch.setattr(runtime, 'client_for', lambda *args: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def test_bedrock_selection_needs_no_personal_key_and_keeps_region_and_model(monkeypatch):
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'bedrock')
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', 'bedrock')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', '')
    monkeypatch.setenv('EVALUATION_JUDGE_MODEL', '')
    monkeypatch.setenv('AWS_REGION', '')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'ap-northeast-3')
    monkeypatch.setenv('QUESTION_GENERATION_API_KEY', 'ignored-personal-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'ignored-personal-key')
    generator, judge = get_question_generator(), get_evaluation_judge()
    assert isinstance(generator, BedrockQuestionGenerator) and isinstance(judge, BedrockEvaluationJudge)
    assert generator.model == judge.model == runtime.DEFAULT_MODEL
    assert judge.descriptor()['region'] == 'ap-northeast-3'
    assert 'ignored-personal-key' not in json.dumps(judge.descriptor())
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'openai')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', 'gpt-test')
    assert get_evaluation_judge().model == runtime.DEFAULT_MODEL
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'bedrock')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', runtime.DEFAULT_MODEL)
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', 'openai')
    from app.services.question_generation import DEFAULT_MODEL as OPENAI_MODEL
    assert get_evaluation_judge().model == OPENAI_MODEL


def test_real_sdk_converse_schema_generates_validated_drafts(stubbed):
    stubbed.add_response('converse', tool_response({'questions': [candidate()]}))
    generated = BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(policies=[], count=1)
    assert len(generated.questions) == 1 and generated.questions[0].expectedConditions[0].ruleKeys == ['refund']


def test_bedrock_transport_uses_iam_chain_with_fixed_timeouts_and_no_retries(monkeypatch):
    calls = []
    class Session:
        def client(self, service, **kwargs):
            calls.append((service, kwargs))
            return object()
    runtime.client_for.cache_clear()
    monkeypatch.setattr(boto3, 'Session', Session)
    try:
        runtime.client_for('ap-northeast-3', 110)
        service, options = calls[0]
        assert service == 'bedrock-runtime' and set(options) == {'region_name', 'config'}
        config = options['config']
        assert config.connect_timeout == 5 and config.read_timeout == 110
        assert config.retries['total_max_attempts'] == 1
        assert config.proxies == {} and config.ignore_configured_endpoint_urls
    finally:
        runtime.client_for.cache_clear()


@pytest.mark.parametrize('aws_code,expected', [('AccessDeniedException', 'AUTH_FAILED'),
    ('ExpiredTokenException', 'AUTH_FAILED'), ('ThrottlingException', 'RATE_LIMITED'),
    ('ModelTimeoutException', 'TIMEOUT'), ('ValidationException', 'INVALID_REQUEST'),
    ('ServiceUnavailableException', 'UNAVAILABLE')])
def test_aws_errors_do_not_leak_or_fall_back_to_personal_apis(stubbed, aws_code, expected):
    stubbed.add_client_error('converse', service_error_code=aws_code, service_message='private-aws-details')
    with pytest.raises(runtime.BedrockError) as error:
        runtime.converse_json(model=runtime.DEFAULT_MODEL, region=runtime.DEFAULT_REGION,
            instructions='채점', payload={}, schema={'type': 'object'}, name='submit_questions',
            max_tokens=128, read_timeout=20)
    assert error.value.code == expected and 'private-aws-details' not in str(error.value)


@pytest.mark.parametrize('reason,code', [('max_tokens', 'GENERATION_INCOMPLETE'),
    ('guardrail_intervened', 'GENERATION_REFUSED'), ('content_filtered', 'GENERATION_REFUSED'),
    ('end_turn', 'GENERATION_SCHEMA_INVALID')])
def test_incomplete_or_refused_output_never_becomes_a_generated_question(stubbed, reason, code):
    stubbed.add_response('converse', tool_response({}, reason=reason))
    with pytest.raises(ApiError) as error:
        BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(policies=[], count=1)
    assert error.value.code == code


def test_wrong_tool_is_rejected(stubbed):
    output = tool_response({'questions': [candidate()]}, name='unrequested-tool')
    stubbed.add_response('converse', output)
    with pytest.raises(ApiError) as error:
        BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(policies=[], count=1)
    assert error.value.code == 'GENERATION_SCHEMA_INVALID'


@pytest.mark.parametrize('questions,count,expected', [
    ([candidate('exception')], 1, 0),
    ([candidate(), candidate()], 2, 1),
    ([candidate(), {**candidate(), 'question': '다른 질문'}], 1, 1),
])
def test_bedrock_trims_invalid_generation_batches(stubbed, questions, count, expected):
    """Off-category, duplicate and extra drafts are dropped; the endpoint refills the shortfall."""
    stubbed.add_response('converse', tool_response({'questions': questions}))
    generated = BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(
        policies=[], count=count, categories=['normal'])
    assert len(generated.questions) == expected


def test_bedrock_allows_a_short_batch_for_endpoint_retry(stubbed):
    stubbed.add_response('converse', tool_response({'questions': [candidate()]}))
    generated = BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(
        policies=[], count=2, categories=['normal'])
    assert len(generated.questions) == 1


def test_oversized_input_does_not_make_a_paid_call(stubbed):
    with pytest.raises(ApiError) as error:
        BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(
            policies=[{'content': '한' * 200000}], count=1)
    assert error.value.code == 'GENERATION_INPUT_TOO_LARGE' and error.value.status == 400


def test_oversized_response_is_rejected_before_creating_questions(stubbed, monkeypatch):
    monkeypatch.setattr(runtime, 'MAX_RESPONSE_BYTES', 10)
    stubbed.add_response('converse', tool_response({'questions': [candidate()]}))
    with pytest.raises(ApiError) as error:
        BedrockQuestionGenerator(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).generate(policies=[], count=1)
    assert error.value.code == 'GENERATION_RESPONSE_TOO_LARGE'


@pytest.mark.parametrize('failure,code', [(NoCredentialsError(), 'AUTH_FAILED'),
    (ReadTimeoutError(endpoint_url='private-endpoint'), 'TIMEOUT')])
def test_sdk_credential_and_timeout_errors_are_sanitized(monkeypatch, failure, code):
    class Client:
        def converse(self, **kwargs):
            raise failure
    monkeypatch.setattr(runtime, 'client_for', lambda *args: Client())
    with pytest.raises(runtime.BedrockError) as error:
        runtime.converse_json(model=runtime.DEFAULT_MODEL, region=runtime.DEFAULT_REGION,
            instructions='생성', payload={}, schema={'type': 'object'}, name='submit_questions',
            max_tokens=128, read_timeout=20)
    assert error.value.code == code and 'private-endpoint' not in str(error.value)


def test_bedrock_normal_worker_keeps_conditions_and_judge_failure_separate(single_ready, monkeypatch):
    factory, client, headers, service, body, original = single_ready
    monkeypatch.setenv('EVALUATION_JUDGE_PROVIDER', 'bedrock')
    monkeypatch.setenv('EVALUATION_JUDGE_MODEL', runtime.DEFAULT_MODEL)
    monkeypatch.setenv('AWS_REGION', runtime.DEFAULT_REGION)

    class Client:
        def converse(self, **request):
            data = json.loads(request['messages'][0]['content'][0]['text'])
            assert request['modelId'] == runtime.DEFAULT_MODEL
            assert request['toolConfig']['toolChoice'] == {'tool': {'name': 'submit_evaluation'}}
            assert all('content' not in policy for policy in data['policies'])
            output = asyncio.run(ConditionJudge().judge(**data))
            return tool_response(output, name='submit_evaluation')

    monkeypatch.setattr(runtime, 'client_for', lambda *args: Client())
    path = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
    assert detail['resultStatus'] == 'MET' and detail['a']['judgeError'] is None
    assert len(detail['a']['conditionResults']) == len(detail['input']['expectedConditions'])

    class Denied:
        def converse(self, **request):
            raise NoCredentialsError()
    monkeypatch.setattr(runtime, 'client_for', lambda *args: Denied())
    failed = submit(client, headers, service, body, key='bedrock-denied')
    asyncio.run(worker.process_one(factory, sender=answer))
    summary = client.get(failed + '/summary', headers=headers).json()['data']
    assert summary['criteria']['policy']['a']['judgeFailed'] == 1
    assert summary['criteria']['policy']['a']['callFailed'] == 0


def test_missing_aws_credentials_are_sanitized_in_judge(monkeypatch):
    def unavailable(**kwargs):
        raise runtime.BedrockError('AUTH_FAILED')
    monkeypatch.setattr(runtime, 'converse_json', unavailable)
    with pytest.raises(JudgeError) as error:
        asyncio.run(BedrockEvaluationJudge(runtime.DEFAULT_MODEL, runtime.DEFAULT_REGION).judge(
            question={'id': 'q'}, answer='답변', evidence={}, policies=[], evidence_catalog=[], criteria={'version': 2}))
    assert error.value.code == 'JUDGE_AUTH_FAILED'
