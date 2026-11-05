"""Consumer-level guarantees for #28/#29 and the #30 analysis boundary."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import evaluation_worker as worker
from app.main import app
from app.models.evaluation import Evaluation, EvaluationItem
from app.services.evaluation_analysis import describe_side, prompt_context, summarize
from app.services.evaluation_judge import get_evaluation_judge
from app.services.evaluations import aggregate
from test_condition_evaluation import ConditionJudge, run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_evaluations import prepare, submit
from test_single_evaluation import single_ready  # noqa: F401


def result(verdict='pass', status='judged'):
    return {'status': status, 'verdict': verdict}


def side(*results, call='SUCCEEDED'):
    return {'callStatus': call, 'criteria': {str(i): value for i, value in enumerate(results)}}


@pytest.mark.parametrize('value,expected', [
    (None, 'REMAINING'), (side(call='NOT_RUN'), 'REMAINING'),
    (side(call='RUNNING'), 'PENDING'), (side(call='FAILED'), 'EXECUTION_ERROR'),
    (side(call='INTERRUPTED'), 'EXECUTION_ERROR'),
    (side(result()), 'CORRECT'), (side(result('fail')), 'INCORRECT'),
    (side(result(None, 'not_evaluable')), 'REVIEW_REQUIRED'),
    (side(result(None, 'error')), 'JUDGMENT_ERROR'),
    (side(result('fail'), result(None, 'error')), 'JUDGMENT_ERROR'),
    (side(result('fail'), result(None, 'not_evaluable')), 'REVIEW_REQUIRED'),
    (side(), 'REVIEW_REQUIRED'),
])
def test_exclusive_screen_status_never_counts_missing_or_error_as_ox(value, expected):
    assert describe_side({}, value)['screenStatus'] == expected


def test_screen_counts_partition_questions_and_compare_sides():
    inputs = {'mode': 'COMPARE'}
    values = [None, side(call='RUNNING'), side(call='FAILED'), side(result()),
              side(result('fail')), side(result(None, 'error')), side(result(None, 'not_evaluable'))]
    items = [SimpleNamespace(id=str(i), input={}, a=value, b=None) for i, value in enumerate(values)]
    summary = summarize(SimpleNamespace(inputs=inputs), items)
    counts = summary['variants']['a']['screenCounts']
    assert counts == {'correct': 1, 'incorrect': 1, 'pending': 1, 'remaining': 1,
                      'executionError': 1, 'judgmentError': 1, 'reviewRequired': 1, 'total': 7}
    assert sum(v for k, v in counts.items() if k != 'total') == counts['total']
    assert summary['variants']['b']['screenCounts']['remaining'] == 7


def test_missing_expected_condition_is_not_a_correct_answer():
    question = {'id': 'q', 'expectedConditions': [{'id': 'c', 'criterion': 'policy',
        'requirement': '필수 안내', 'policyVersionId': 'p', 'sourceQuote': '정책'}]}
    value = describe_side(question, side(result()))
    assert value['screenStatus'] == 'REVIEW_REQUIRED'


def test_score_mean_excludes_missing_call_failed_and_unjudged(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    with factory() as db:
        run = db.get(Evaluation, path.rsplit('/', 1)[-1])
        original = db.scalar(select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id))
        rows = [SimpleNamespace(a=deepcopy(original.a), b=None) for _ in range(5)]
        rows[0].a['criteria']['questionResolution'].update(score=0, verdict='fail')
        rows[1].a['criteria']['questionResolution'].update(score=4, verdict='pass')
        rows[2].a['criteria']['questionResolution'].update(score=None, verdict=None, status='not_evaluable')
        rows[3].a['callStatus'] = 'FAILED'
        rows[4].a = None
        data = aggregate(run, rows)['criteria']['questionResolution']['a']
    assert data['scoreSummary'] == {'average': 2, 'denominator': 2, 'minimum': 0, 'maximum': 4,
                                    'passThreshold': 3, 'basis': 'AUTOMATIC'}
    assert data['denominator'] == 2 and data['passRate'] == 50
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['criteria']['policy']['a']['scoreSummary'] is None
    assert summary['criteria']['questionResolution']['a']['scoreSummary']['average'] == 4


class HypothesisJudge(FailingConditionJudge):
    def __init__(self, areas=('MODEL',)):
        self.areas = areas

    async def judge(self, **kwargs):
        output = await super().judge(**kwargs)
        for i, decision in enumerate(output['conditionResults']):
            decision['verdict'] = 'fail'
            decision['issue'] = {'kind': 'condition_omitted', 'missingGuidance': ['필수 안내'],
                'recommendation': '필수 안내 확인', 'hypothesis': {'area': self.areas[i % len(self.areas)],
                    'reason': '관측 근거에 기반한 가설이며 원인 확정 아님', 'evidenceRefs': decision['evidence_refs']}}
        return output


def test_cause_counts_deduplicate_questions_and_reviews_remove_active_hypotheses(single_ready):
    path, detail = run_condition(single_ready, HypothesisJudge())
    _, client, headers, *_ = single_ready
    analysis = detail['analysis']['a']
    assert len(analysis['hypotheses']) > 1
    assert analysis['confirmedCause'] is None
    assert analysis['analysisStatus'] == 'HYPOTHESIS_AVAILABLE'
    summary = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']
    assert summary['causeQuestionCounts'] == {'MODEL': 1, 'PROMPT': 0, 'RETRIEVAL': 0}
    assert summary['analysisStatusCounts']['HYPOTHESIS_AVAILABLE'] == 1
    params = {'causeArea': 'MODEL', 'screenStatus': 'INCORRECT', 'analysisStatus': 'HYPOTHESIS_AVAILABLE'}
    assert len(client.get(path + '/items', headers=headers, params=params).json()['data']['items']) == 1
    assert client.get(path + '/items', headers=headers, params={'causeArea': 'PROMPT'}).json()['data']['items'] == []
    endpoint = path + '/items/' + detail['id']
    for revision, condition in enumerate(detail['input']['expectedConditions']):
        response = client.post(endpoint + '/reviews', headers=headers, json={
            'expectedRevision': revision, 'conditionId': condition['id'], 'verdict': 'pass', 'reason': '원문 재확인'})
        assert response.status_code == 201
    updated = client.get(endpoint, headers=headers).json()['data']['analysis']['a']
    assert updated['hypotheses'] == analysis['hypotheses']  # Immutable automatic context.
    assert updated['activeHypotheses'] == [] and updated['analysisStatus'] == 'NOT_APPLICABLE'
    assert client.get(path + '/items', headers=headers, params=params).json()['data']['items'] == []
    assert client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['causeQuestionCounts']['MODEL'] == 0


@pytest.mark.parametrize('area', ['RETRIEVAL', 'PROMPT'])
def test_unobserved_source_cannot_supply_a_cause_filter(single_ready, area):
    path, detail = run_condition(single_ready, HypothesisJudge((area,)))
    _, client, headers, *_ = single_ready
    assert detail['analysis']['a']['analysisStatus'] == 'UNDETERMINED'
    assert detail['analysis']['a']['activeHypotheses'] == []
    assert client.get(path + '/items', headers=headers, params={'causeArea': area}).json()['data']['items'] == []


def test_prompt_context_uses_frozen_selected_or_configured_text_only():
    selected = {'id': 'pv', 'promptId': 'p', 'version': 2, 'content': '고정 프롬프트'}
    inputs = {'prompts': {'a': selected}, 'connection': {'revision': 3}, 'comparisonAssurance': {}}
    context = prompt_context(inputs, 'a')
    assert context['source'] == 'SELECTED_VERSION' and context['content'] == selected['content']
    assert context['promptVersionId'] == 'pv' and context['applicationVerified'] is False
    inputs = {'useConfiguredPrompt': True, 'connection': {'revision': 3, 'config': {
        'executionMode': 'PROMPT_BODY', 'promptPointer': '/system', 'requestTemplate': {'system': '설정 원문', 'private': '다른 필드'}}}}
    context = prompt_context(inputs, 'a')
    assert context['source'] == 'CONFIGURED_TEMPLATE' and context['content'] == '설정 원문'
    assert '다른 필드' not in str(context) and context['promptVersionId'] is None
    inputs['connection']['config']['executionMode'] = 'CONNECTION_ONLY'
    assert prompt_context(inputs, 'a')['available'] is False


def test_queued_cancelled_and_ungraded_api_counts(single_ready):
    factory, client, headers, service, body, _ = single_ready
    path = submit(client, headers, service, body)
    for _ in range(2):
        counts = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['screenCounts']
        assert counts['remaining'] == 1 and counts['correct'] == counts['incorrect'] == 0
        client.post(path + '/cancel', headers=headers, json={})
    assert client.post(path + '/resume', headers=headers, json={'connectionRevision': body['connectionRevision']}).status_code == 202
    async def sender(*args):
        return '판정 모델이 없는 답변'
    asyncio.run(worker.process_one(factory, sender=sender))
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['evaluation']['completedQuestions'] == 1
    assert summary['analysis']['variants']['a']['screenCounts']['reviewRequired'] == 1
    assert summary['criteria']['questionResolution']['a']['scoreSummary']['average'] is None
    assert summary['criteria']['questionResolution']['a']['scoreSummary']['denominator'] == 0
    assert client.get(path + '/items', headers=headers, params={'side': 'b'}).status_code == 400
    assert client.get(path + '/items', headers=headers, params={'causeArea': 'INVALID'}).status_code == 400


def test_cause_filter_pagination_is_scoped_and_counts_whole_run(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, original, _ = prepare(factory, client, monkeypatch, question_count=3)
    judge = HypothesisJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, {k: original[k] for k in ('connectionRevision', 'testSetVersionId')})
    async def sender(*args):
        return '기업 답변'
    asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    params = {'causeArea': 'MODEL', 'limit': 1}
    first = client.get(path + '/items', headers=headers, params=params).json()['data']
    assert len(first['items']) == 1 and first['nextCursor']
    second = client.get(path + '/items', headers=headers, params={**params, 'cursor': first['nextCursor']}).json()['data']
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert client.get(path + '/items', headers=headers, params={**params, 'causeArea': 'PROMPT', 'cursor': first['nextCursor']}).status_code == 400
    assert client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['causeQuestionCounts']['MODEL'] == 3


def test_compare_filters_each_side_and_counts_overlapping_cause_areas(single_ready):
    from app.services.chatbot_gateway import ChatbotReply
    factory, client, headers, service, _, original = single_ready
    judge = HypothesisJudge(('RETRIEVAL', 'PROMPT'))
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, {**original, 'mode': 'COMPARE'})
    async def sender(config, payload, outgoing):
        # A telemetry absent; B explicitly retrieved zero documents.
        return ChatbotReply('기업 답변', {'documents': None if payload['messages'][0]['content'] == 'A prompt' else [], 'citations': None})
    asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    assert client.get(path + '/items', headers=headers, params={'side': 'a', 'causeArea': 'RETRIEVAL'}).json()['data']['items'] == []
    page = client.get(path + '/items', headers=headers, params={'side': 'b', 'causeArea': 'RETRIEVAL'}).json()['data']
    assert len(page['items']) == 1 and set(page['items'][0]['activeHypothesisAreas']) == {'RETRIEVAL', 'PROMPT'}
    detail = client.get(path + '/items/' + page['items'][0]['id'], headers=headers).json()['data']
    assert detail['analysis']['b']['promptContext']['content'] == 'B prompt'
    assert detail['analysis']['b']['facts']['retrievedDocumentCount'] == 0
    assert detail['analysis']['b']['confirmedCause'] is None
    counts = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']
    assert counts['b']['causeQuestionCounts'] == {'RETRIEVAL': 1, 'PROMPT': 1, 'MODEL': 0}
    assert counts['b']['screenCounts']['incorrect'] == 1
    assert counts['a']['causeQuestionCounts']['RETRIEVAL'] == 0
