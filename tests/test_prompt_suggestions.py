"""Offline integration from observed failure through immutable version and existing reruns."""
import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app import evaluation_worker as worker
from app.main import app
from app.models.evaluation import Evaluation
from app.models.prompt import PromptVersion
from app.models.prompt_suggestion import PromptSuggestion
from app.services.auth import now
from app.services.evaluation_judge import get_evaluation_judge
from app.services.prompt_suggestion_generator import get_suggestion_generator
from test_condition_evaluation import ConditionJudge, run_condition, answer
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401


class FakeGenerator:
    def __init__(self):
        self.calls = 0
        self.transform = lambda value: value

    def descriptor(self):
        return {'provider': 'test', 'model': 'offline'}

    def generate(self, payload):
        self.calls += 1
        assert payload['evidence'][0]['condition']['judgment']['verdict'] == 'fail'
        return self.transform({'suggestions': [{'originalText': payload['content'],
            'replacementText': payload['content'] + '\n정책의 취소 기한을 확인하고 안내하세요.',
            'reason': '관측한 취소 기한 누락을 줄이기 위한 가설이며 재시험이 필요합니다.',
            'evidenceIds': [e['id'] for e in payload['evidence']]}]})


def setup_suggestion(single_ready):
    factory, client, headers, root, body, original = single_ready
    body['promptVersionId'] = original['bPromptVersionId']
    source, item = run_condition(single_ready, FailingConditionJudge())
    generator = FakeGenerator()
    app.dependency_overrides[get_suggestion_generator] = lambda: generator
    request = {'evaluationId': source.rsplit('/', 1)[1], 'baseVersionId': body['promptVersionId'],
               'itemIds': [item['id']]}
    return factory, client, headers, root, source, item, generator, request


@pytest.fixture
def suggested(single_ready):
    return setup_suggestion(single_ready)


def create(suggested, *, key='suggestion-1', body=None):
    _, client, headers, root, _, _, _, request = suggested
    return client.post(root + '/prompt-suggestions', headers={**headers, 'Idempotency-Key': key}, json=body or request)


def select_all(suggested, response=None):
    _, client, headers, *_ = suggested
    response = response or create(suggested)
    assert response.status_code == 201, response.text
    row = response.json()['data']
    selected = client.patch(response.headers['location'], headers=headers, json={'expectedRevision': row['revision'],
        'selectedSuggestionIds': [s['id'] for s in row['suggestions']]})
    assert selected.status_code == 200, selected.text
    return response.headers['location'], selected.json()['data']


def application(row):
    return {'baseVersionId': row['baseVersionId'], 'content': row['previewContent'],
            'suggestionId': row['id'], 'suggestionRevision': row['revision'], 'changeNote': '검토한 수정안 적용'}


def apply(suggested, row, **changes):
    _, client, headers, *_ = suggested
    return client.post(row['links']['saveVersion'], headers=headers, json={**application(row), **changes})


def test_full_workflow_reuses_version_rerun_and_comparison(suggested):
    factory, client, headers, root, source, item, generator, request = suggested
    generated = create(suggested)
    assert generated.status_code == 201, generated.text
    initial = generated.json()['data']
    assert initial['status'] == 'READY' and initial['selectedSuggestionIds'] == []
    assert initial['estimate']['expectedImprovedQuestionCount'] is None
    assert initial['estimate']['targetedQuestionCount'] == 0
    evidence = initial['snapshot']['evidence'][0]
    assert evidence['itemId'] == item['id'] and evidence['questionId'] == item['questionId']
    assert evidence['condition']['policyEvidence']['quote'] == item['input']['evidenceQuote']
    assert initial['suggestions'][0]['originalText'] == 'B prompt'
    assert create(suggested).json()['data']['id'] == initial['id'] and generator.calls == 1
    path, selected = select_all(suggested, generated)
    assert selected['estimate']['targetedQuestionCount'] == 1
    assert selected['estimate']['method'] == 'NOT_ESTIMABLE'
    saved = apply(suggested, selected)
    assert saved.status_code == 201, saved.text
    version = saved.json()['data']
    assert version['content'] == selected['previewContent'] and version['version'] == 3
    assert apply(suggested, selected).json()['data']['id'] == version['id']
    applied = client.get(path, headers=headers).json()['data']
    assert applied['status'] == 'APPLIED' and applied['appliedPromptVersionId'] == version['id']
    assert client.get(applied['links']['results'], headers=headers).json()['data']['items'] == []
    assert client.get(root + '/prompt-suggestions', headers=headers).json()['data']['items'][0]['id'] == initial['id']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PromptVersion).where(
            PromptVersion.prompt_id == selected['snapshot']['promptId'])) == 3
    judge = ConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    rerun_body = {'connectionRevision': 2, 'promptVersionId': version['id']}
    rerun = client.post(applied['links']['rerun'], headers={**headers, 'Idempotency-Key': 'new-version'}, json=rerun_body)
    assert rerun.status_code == 202, rerun.text
    assert client.post(applied['links']['rerun'], headers={**headers, 'Idempotency-Key': 'new-version'},
                       json=rerun_body).json()['data']['id'] == rerun.json()['data']['id']
    pending = client.get(applied['links']['results'], headers=headers).json()['data']['items'][0]
    assert pending['comparisonAvailable'] is False
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    result = client.get(applied['links']['results'], headers=headers).json()['data']['items'][0]
    assert result['comparisonAvailable'] is True
    comparison = client.get(result['comparisonUrl'], headers=headers).json()['data']
    assert comparison['outcome'] == 'IMPROVED' and comparison['counts']['RESOLVED'] == 1
    assert comparison['countUnit'] == 'CONDITION' and comparison['externalEnvironmentControlled'] is False
    before = client.get(source + '/inputs', headers=headers).json()['data']
    after = client.get(rerun.headers['location'] + '/inputs', headers=headers).json()['data']
    assert before['testSet'] == after['testSet'] and before['criteria'] == after['criteria']
    assert after['prompts']['a']['id'] == version['id']
    # Actual results must never overwrite the pre-test estimate.
    assert client.get(path, headers=headers).json()['data']['estimate'] == selected['estimate']


@pytest.mark.parametrize('fault', ['invented_anchor', 'foreign_evidence', 'empty_reason', 'no_change',
                                   'invalid_schema', 'invented_prediction', 'blank_prompt', 'oversize'])
def test_invalid_model_output_is_durable_and_does_not_retry(suggested, fault):
    factory, client, headers, root, source, item, generator, request = suggested
    def invalid(value):
        edit = value['suggestions'][0]
        if fault == 'invented_anchor': edit['originalText'] = '원문에 없는 내용'
        if fault == 'foreign_evidence': edit['evidenceIds'] = ['foreign']
        if fault == 'empty_reason': edit['reason'] = ' '
        if fault == 'no_change': edit['replacementText'] = edit['originalText']
        if fault == 'invalid_schema': return {'raw': 'not a proposal'}
        if fault == 'invented_prediction': value['predictedImprovement'] = 99
        if fault == 'blank_prompt': edit['replacementText'] = ' '
        if fault == 'oversize': edit['replacementText'] = 'x' * 100_001
        return value
    generator.transform = invalid
    response = create(suggested)
    row = response.json()['data']
    assert row['status'] == 'FAILED' and row['suggestions'] == []
    assert row['error']['code'] == 'INVALID_SUGGESTION_OUTPUT'
    assert create(suggested).json()['data']['id'] == row['id'] and generator.calls == 1
    assert client.patch(response.headers['location'], headers=headers,
        json={'expectedRevision': 1, 'selectedSuggestionIds': []}).status_code == 409


@pytest.mark.parametrize('kind', ['TIMEOUT', 'AUTH_FAILED', 'REFUSED', 'INVALID_OUTPUT'])
def test_provider_failure_is_sanitized_and_not_retried(suggested, kind):
    from app.core.errors import ApiError
    generator = suggested[6]
    def failure(value):
        raise ApiError(502, 'SUGGESTION_' + kind, '안전한 오류 메시지')
    generator.transform = failure
    first = create(suggested).json()['data']
    assert first['status'] == 'FAILED' and first['error']['code'] == 'SUGGESTION_' + kind
    assert create(suggested).json()['data']['id'] == first['id'] and generator.calls == 1


def test_permissions_request_conflicts_and_selection_revisions(suggested):
    from test_projects import register
    factory, client, headers, root, source, item, generator, request = suggested
    response = create(suggested)
    row = response.json()['data']
    path = response.headers['location']
    other = register(client, 'suggestion_outsider')
    for suffix in ('', '/results'):
        assert client.get(path + suffix).status_code == 401
        assert client.get(path + suffix, headers=other).status_code == 404
    assert client.post(root + '/prompt-suggestions', headers={**other, 'Idempotency-Key': 'foreign'}, json=request).status_code == 404
    assert client.patch(path, headers=other, json={'expectedRevision': 1, 'selectedSuggestionIds': []}).status_code == 404
    assert client.post(row['links']['saveVersion'], headers=other, json=application(row)).status_code == 404
    assert create(suggested, body={**request, 'baseVersionId': 'other'}).status_code == 409
    assert create(suggested, key='foreign-item', body={**request, 'itemIds': ['foreign']}).status_code == 404
    assert create(suggested, key='duplicate', body={**request, 'itemIds': request['itemIds'] * 2}).status_code == 400
    edit_id = row['suggestions'][0]['id']
    for ids in (['foreign'], [edit_id, edit_id]):
        assert client.patch(path, headers=headers, json={'expectedRevision': 1, 'selectedSuggestionIds': ids}).status_code == 400
    assert client.patch(path, headers=headers, json={'expectedRevision': 1, 'selectedSuggestionIds': [edit_id]}).status_code == 200
    assert client.patch(path, headers=headers, json={'expectedRevision': 1, 'selectedSuggestionIds': []}).status_code == 409
    assert apply(suggested, row).status_code == 409


def test_overlapping_alternatives_and_unique_target_count(suggested):
    generator = suggested[6]
    def alternatives(value):
        alternative = deepcopy(value['suggestions'][0])
        alternative['replacementText'] += '\n다른 대안'
        value['suggestions'].append(alternative)
        return value
    generator.transform = alternatives
    response = create(suggested)
    row = response.json()['data']
    _, client, headers, *_ = suggested
    assert client.patch(response.headers['location'], headers=headers, json={'expectedRevision': 1,
        'selectedSuggestionIds': [s['id'] for s in row['suggestions']]}).json()['error']['code'] == 'OVERLAPPING_SUGGESTIONS'
    selected = client.patch(response.headers['location'], headers=headers, json={'expectedRevision': 1,
        'selectedSuggestionIds': [row['suggestions'][0]['id']]}).json()['data']
    assert selected['estimate']['targetedQuestionCount'] == 1


def test_apply_content_no_selection_and_version_conflict_roll_back(suggested):
    _, client, headers, *_ = suggested
    response = create(suggested)
    row = response.json()['data']
    assert apply(suggested, row).json()['error']['code'] == 'SUGGESTION_SELECTION_REQUIRED'
    path, selected = select_all(suggested, response)
    assert apply(suggested, selected, content='임의 변경').json()['error']['code'] == 'SUGGESTION_CONTENT_MISMATCH'
    ordinary = client.post(selected['links']['saveVersion'], headers=headers,
        json={'baseVersionId': selected['baseVersionId'], 'content': '다른 사용자의 수정'})
    assert ordinary.status_code == 201
    assert apply(suggested, selected).json()['error']['code'] == 'PROMPT_VERSION_CONFLICT'
    unchanged = client.get(path, headers=headers).json()['data']
    assert unchanged['status'] == 'READY' and unchanged['revision'] == selected['revision']
    assert unchanged['appliedPromptVersionId'] is None


def test_human_review_change_blocks_stale_application(suggested):
    _, client, headers, root, source, item, *_ = suggested
    path, selected = select_all(suggested)
    result = client.post(source + '/items/' + item['id'] + '/reviews', headers=headers,
        json={'expectedRevision': item['reviewRevision'], 'side': 'a',
              'conditionId': item['input']['expectedConditions'][0]['id'], 'verdict': 'fail', 'reason': '추가 관측으로 변경'})
    assert result.status_code == 201, result.text
    assert apply(suggested, selected).json()['error']['code'] == 'SUGGESTION_EVIDENCE_CHANGED'
    assert client.get(path, headers=headers).json()['data']['status'] == 'READY'


@pytest.mark.parametrize('mutation,code', [('running', 'INVALID_SUGGESTION_SOURCE'),
    ('compare', 'INVALID_SUGGESTION_SOURCE'), ('v1', 'INVALID_SUGGESTION_SOURCE'),
    ('no_prompt', 'SUGGESTION_PROMPT_MISMATCH'), ('no_failure', 'NO_FAILED_CONDITION'),
    ('call_failed', 'NO_FAILED_CONDITION'), ('judge_failed', 'NO_FAILED_CONDITION')])
def test_invalid_analysis_source_never_calls_model(suggested, mutation, code):
    from app.models.evaluation import EvaluationItem
    factory, _, _, _, source, item, generator, request = suggested
    with factory() as db:
        run = db.get(Evaluation, request['evaluationId'])
        inputs = deepcopy(run.inputs)
        if mutation == 'running': run.status = 'RUNNING'
        if mutation == 'compare': inputs['mode'] = 'COMPARE'
        if mutation == 'v1': inputs['criteria']['version'] = 1
        if mutation == 'no_prompt': inputs['prompts'] = {}
        run.inputs = inputs
        entry = db.get(EvaluationItem, item['id'])
        side = deepcopy(entry.a)
        if mutation == 'call_failed': side['callStatus'] = 'FAILED'
        if mutation in ('no_failure', 'judge_failed'):
            for condition in side['conditionResults']:
                condition.update(verdict='pass' if mutation == 'no_failure' else None,
                                 status='judged' if mutation == 'no_failure' else 'error', issue=None)
        entry.a = side
        db.commit()
    response = create(suggested)
    assert response.status_code == 409 and response.json()['error']['code'] == code
    assert generator.calls == 0


def test_interrupted_generation_does_not_repeat_paid_call(suggested):
    factory, client, headers, root, source, item, generator, request = suggested
    response = create(suggested)
    with factory() as db:
        row = db.get(PromptSuggestion, response.json()['data']['id'])
        row.status, row.suggestions = 'GENERATING', []
        row.created_at = now() - timedelta(minutes=6)
        db.commit()
    expired = client.get(response.headers['location'], headers=headers).json()['data']
    assert expired['status'] == 'FAILED' and expired['error']['code'] == 'SUGGESTION_INTERRUPTED'
    assert create(suggested).json()['data']['status'] == 'FAILED' and generator.calls == 1


@pytest.mark.parametrize('change', ['revision', 'config', 'judge', 'mode'])
def test_existing_rerun_constraints_are_preserved(suggested, change):
    from app.models.connection import Connection
    factory, client, headers, *_ = suggested
    _, selected = select_all(suggested)
    saved = apply(suggested, selected).json()['data']
    body = {'connectionRevision': 2, 'promptVersionId': saved['id']}
    if change == 'revision': body['connectionRevision'] = 1
    elif change == 'judge': app.dependency_overrides[get_evaluation_judge] = lambda: type('Changed', (), {'descriptor': lambda _: {'model': 'changed'}})()
    else:
        with factory() as db:
            connection = db.scalar(select(Connection))
            config = deepcopy(connection.config)
            if change == 'mode': config['executionMode'] = 'CONNECTION_ONLY'
            else: config['timeoutMs'] = 999
            connection.config = config
            db.commit()
    response = client.post(selected['links']['rerun'], headers={**headers, 'Idempotency-Key': 'constraint'}, json=body)
    assert response.status_code == 409
    assert client.get(selected['links']['results'], headers=headers).json()['data']['items'] == []


def test_bedrock_adapter_reuses_settings_and_never_exposes_raw_errors(monkeypatch):
    from app.services import bedrock_runtime
    from app.services.prompt_suggestion_generator import get_suggestion_generator
    from app.core.errors import ApiError
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'bedrock')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', 'configured-model')
    monkeypatch.setenv('AWS_REGION', 'ap-northeast-3')
    calls = []
    def fake(**kwargs):
        calls.append(kwargs)
        raise bedrock_runtime.BedrockError('TIMEOUT')
    monkeypatch.setattr(bedrock_runtime, 'converse_json', fake)
    generator = get_suggestion_generator()
    with pytest.raises(ApiError) as error:
        generator.generate({'content': '원문', 'evidence': []})
    assert error.value.code == 'SUGGESTION_TIMEOUT' and len(calls) == 1
    assert calls[0]['model'] == 'configured-model' and calls[0]['read_timeout'] == 55
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', '')
    with pytest.raises(ApiError) as error:
        get_suggestion_generator().generate({})
    assert error.value.code == 'SUGGESTION_NOT_CONFIGURED' and len(calls) == 1


@pytest.mark.parametrize('outcome', ['MIXED', 'INCOMPLETE'])
def test_actual_regressions_and_incomparable_conditions_remain_visible(suggested, outcome):
    factory, client, headers, *_ = suggested
    _, selected = select_all(suggested)
    version = apply(suggested, selected).json()['data']
    class ResultJudge(ConditionJudge):
        async def judge(self, **kwargs):
            result = await super().judge(**kwargs)
            if outcome == 'INCOMPLETE':
                result['policy']['question_id'] = 'invalid'
            else:
                result['policy']['verdict'] = 'fail'
                result['conditionResults'][1].update(verdict='fail', reason='새 금지 행동 위반',
                    issue={'kind': 'policy_contradiction', 'answerQuote': None,
                           'missingGuidance': ['금지 행동'], 'recommendation': '금지 행동 준수'})
            return result
    judge = ResultJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    rerun = client.post(selected['links']['rerun'], headers={**headers, 'Idempotency-Key': 'actual'},
        json={'connectionRevision': 2, 'promptVersionId': version['id']})
    assert rerun.status_code == 202, rerun.text
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    actual = client.get(rerun.headers['location'] + '/comparison', headers=headers).json()['data']
    assert actual['outcome'] == outcome
    if outcome == 'MIXED':
        assert actual['counts']['RESOLVED'] == 1 and actual['counts']['NEW'] == 1
    else:
        assert actual['counts']['NOT_COMPARABLE'] == 2 and actual['counts']['RESOLVED'] == 0


def test_duplicate_inflight_generation_returns_same_resource_without_call(suggested):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    generator = suggested[6]
    entered, release = Event(), Event()
    def delayed(value):
        entered.set()
        assert release.wait(10)
        return value
    generator.transform = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create, suggested)
        try:
            assert entered.wait(10)
            duplicate = create(suggested)
            assert duplicate.status_code == 202 and duplicate.json()['data']['status'] == 'GENERATING'
        finally:
            release.set()
        original = future.result(timeout=10)
    assert original.json()['data']['id'] == duplicate.json()['data']['id'] and generator.calls == 1


def test_generation_and_result_pagination_and_openapi(suggested):
    _, client, headers, root, *_ = suggested
    first, second = create(suggested), create(suggested, key='second')
    response = client.get(root + '/prompt-suggestions?limit=1', headers=headers).json()['data']
    assert response['nextCursor'] and len(response['items']) == 1
    rest = client.get(root + '/prompt-suggestions', headers=headers,
        params={'limit': 1, 'cursor': response['nextCursor']}).json()['data']
    assert rest['items'][0]['id'] != response['items'][0]['id'] and rest['nextCursor'] is None
    assert client.get(root + '/prompt-suggestions?cursor=invalid', headers=headers).status_code == 400
    schema = client.get('/openapi.json').json()
    definitions = schema['components']['schemas']
    assert 'suggestionId' in definitions['PromptVersionCreate']['properties']
    assert definitions['EffectEstimate']['properties']['expectedImprovedQuestionCount']['type'] == 'null'
    assert 'suggestions' in definitions['SuggestionResponse']['properties']
    assert client.get(root + '/prompt-suggestions/capabilities', headers=headers).json()['data']['expectedImprovementCountSupported'] is False


def test_project_purge_removes_suggestions_before_prompt_versions(suggested):
    from app.models.project import Project
    from app.services.project_lifecycle import purge_expired_projects
    factory, client, headers, root, *_ = suggested
    path, selected = select_all(suggested)
    assert apply(suggested, selected).status_code == 201
    project = root.split('/services/')[0]
    assert client.delete(project, headers={**headers, 'If-Match': '"1"'}).status_code == 204
    assert client.get(path, headers=headers).status_code == 404
    with factory() as db:
        row = db.get(Project, project.rsplit('/', 1)[1])
        row.deleted_at = now() - timedelta(days=31)
        db.commit()
        assert purge_expired_projects(db) == 1
        db.commit()
        assert db.get(PromptSuggestion, selected['id']) is None


def test_unexpected_adapter_exception_never_leaks_payload(suggested):
    def bad(value):
        raise RuntimeError('secret-provider-body')
    suggested[6].transform = bad
    response = create(suggested)
    assert response.json()['data']['status'] == 'FAILED'
    assert 'secret-provider-body' not in response.text


def test_unicode_offsets_and_ambiguous_anchor_validation():
    from app.services.prompt_suggestions import validate_generated
    base = {'evidence': [{'id': 'e1'}], 'content': '🙂안내\n정책을 따르세요.'}
    edit = {'originalText': '정책', 'replacementText': '제공된 정책', 'reason': '근거 확인', 'evidenceIds': ['e1']}
    result = validate_generated({'suggestions': [edit]}, base)[0]
    assert result['start'] == 4 and result['end'] == 6
    with pytest.raises(ValueError):
        validate_generated({'suggestions': [{**edit, 'originalText': 'aaa'}]}, {**base, 'content': 'aaaa'})


@pytest.mark.parametrize('state', ['answer_conflict', 'review_pending', 'mixed_judge_error'])
def test_screen_review_and_error_states_are_not_prompt_improvement_targets(suggested, state):
    """#30 must consume #28/#29's current review-aware wrong-answer contract."""
    from app.models.evaluation import EvaluationItem
    factory, client, headers, _, source, item, generator, _ = suggested
    if state == 'mixed_judge_error':
        with factory() as db:
            row = db.get(EvaluationItem, item['id'])
            value = deepcopy(row.a)
            value['criteria']['communicationQuality'].update(status='error', verdict=None, score=None,
                evidence_refs=[], error={'code': 'JUDGE_FAILED', 'message': '판정 실패'})
            row.a = value
            db.commit()
    else:
        saved = client.post(source + '/items/' + item['id'] + '/answer-reviews', headers=headers, json={
            'expectedRevision': 0, 'verdict': 'pass' if state == 'answer_conflict' else 'not_evaluable'})
        assert saved.status_code == 201, saved.text
    response = create(suggested)
    assert response.status_code == 409 and response.json()['error']['code'] == 'NO_FAILED_CONDITION'
    assert generator.calls == 0
