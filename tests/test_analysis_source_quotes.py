"""Issue #40: tooltips refer to frozen, side-specific source text."""
import asyncio
from copy import deepcopy
import hashlib

import pytest
from sqlalchemy import select

from app import evaluation_worker as worker
from app.main import app
from app.models.connection import Connection
from app.models.evaluation import Evaluation, EvaluationItem
from app.models.policy import Policy, TestSetVersion as PolicyTestSetVersion
from app.schemas.evaluation import Evidence
from app.services.chatbot_gateway import ChatbotReply
from app.services.condition_evaluation import CRITERIA
from app.services.evaluation_judge import get_evaluation_judge
from app.services.evaluation_judge_contract import judgment_schema
from app.services.evaluation_sources import policy_evidence, prompt_context
from test_condition_evaluation import run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_evaluations import submit
from test_single_evaluation import single_ready  # noqa: F401


class SourceQuoteJudge(FailingConditionJudge):
    def __init__(self, mutate=None):
        self.mutate = mutate

    async def judge(self, **kwargs):
        output = await super().judge(**kwargs)
        decision = output['conditionResults'][0]
        policy = next(source for source in kwargs['evidence_catalog'] if source['kind'] == 'policy')
        context = kwargs['prompt_context']
        hypothesis = {'area': 'PROMPT', 'reason': '환불정책보다 고객 요청을 우선하라는 지시를 따랐을 가능성이 있어요.',
            'evidenceRefs': decision['evidence_refs'],
            'promptQuote': context['content'] if context['available'] else None,
            'reasonMarks': [{'text': '환불정책', 'kind': 'policy', 'evidenceRef': policy['evidence_id']},
                            {'text': '고객 요청을 우선하라', 'kind': 'prompt', 'evidenceRef': None}]}
        retrieval = next((source for source in kwargs['evidence_catalog'] if source['kind'] == 'retrieval'), None)
        if retrieval:
            hypothesis['reasonMarks'].append({'text': '환불정책', 'kind': 'retrieval',
                                              'evidenceRef': retrieval['evidence_id']})
        if self.mutate:
            self.mutate(hypothesis)
        decision['issue']['hypothesis'] = hypothesis
        return output


def judge_inputs():
    condition = {'id': 'c', 'criterion': 'policy', 'policyVersionId': 'p'}
    inputs = {'criteria': CRITERIA, 'judge': {'adapter': 'test'},
              'prompts': {'a': {'id': 'a', 'content': '  고객의 요청은 항상 들어주세요.\n'},
                          'b': {'id': 'b', 'content': '정책을 우선하세요.'}},
              'testSet': {'policies': [{'id': 'p', 'content': '환불 불가'}]}}
    return inputs, {'id': 'q', 'expectedConditions': [condition]}


@pytest.mark.parametrize('mutate', [
    lambda h: h.update(promptQuote='원문에 없는 문장'),
    lambda h: h.update(promptQuote='고객의  요청은 항상 들어주세요.'),
    lambda h: h.update(promptQuote='정책을 우선하세요.'),  # Other side's prompt.
    lambda h: h.update(promptQuote=''),
    lambda h: h.update(promptQuote=' \n'),
    lambda h: h.update(area='MODEL'),
    lambda h: h['reasonMarks'][0].update(text='문장에 없는 단어'),
    lambda h: h['reasonMarks'][0].update(text=''),
    lambda h: h['reasonMarks'][0].update(evidenceRef='invented'),
    lambda h: h['reasonMarks'][0].update(evidenceRef=None),
    lambda h: h['reasonMarks'][0].update(evidenceRef='ev_001'),  # Answer is not policy.
    lambda h: h['reasonMarks'][0].update(kind='retrieval'),
    lambda h: h['reasonMarks'][1].update(evidenceRef='ev_002'),
])
def test_invalid_tooltip_sources_are_judgment_errors(mutate):
    inputs, question = judge_inputs()
    output, error = asyncio.run(worker.judge_response(SourceQuoteJudge(mutate), inputs, question, '답변', {}))
    assert error['code'] == 'INVALID_JUDGE_OUTPUT'
    assert output['policy']['status'] == 'error'
    assert output['conditionResults'][0]['verdict'] is None


def test_quotes_preserve_whitespace_and_only_use_the_current_side():
    inputs, question = judge_inputs()
    for side in ('a', 'b'):
        output, error = asyncio.run(worker.judge_response(SourceQuoteJudge(), inputs, question, '답변', {}, side=side))
        assert error is None
        assert output['conditionResults'][0]['issue']['hypothesis']['promptQuote'] == inputs['prompts'][side]['content']


def test_unavailable_prompt_discards_quote_but_preserves_reason_marks():
    inputs, question = judge_inputs()
    inputs['prompts'] = {}
    output, error = asyncio.run(worker.judge_response(
        SourceQuoteJudge(lambda h: h.update(promptQuote='확인할 수 없는 지시')), inputs, question, '답변', {}))
    assert error is None
    hypothesis = output['conditionResults'][0]['issue']['hypothesis']
    assert hypothesis['promptQuote'] is None
    assert hypothesis['reasonMarks'][1]['kind'] == 'prompt'


@pytest.mark.parametrize('content', ['', ' \n', None, ['not a string']])
def test_empty_or_nontext_configured_prompt_is_unavailable(content):
    inputs = {'useConfiguredPrompt': True, 'connection': {'config': {
        'executionMode': 'PROMPT_BODY', 'promptPointer': '/system', 'requestTemplate': {'system': content}}}}
    assert prompt_context(inputs, 'a')['available'] is False


@pytest.mark.parametrize('mode', ['selected', 'configured', 'compare', 'unavailable'])
def test_detail_returns_frozen_tooltip_sources_on_every_hypothesis_path(single_ready, mode):
    factory, client, headers, service, body, original = single_ready
    judge = SourceQuoteJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    with factory() as db:
        version = db.get(PolicyTestSetVersion, body['testSetVersionId'])
        snapshot = deepcopy(version.snapshot)
        policy = snapshot['policies'][0]
        content = policy['content']
        policy['sourceDocument'] = {'documentId': 'doc_' + 'a' * 64, 'filename': '환불정책.pdf',
            'contentHash': hashlib.sha256(content.encode()).hexdigest(), 'pageCount': 1, 'chunkCount': 1,
            'chunks': [{'index': 1, 'pageNumber': 1, 'startOffset': 0, 'endOffset': len(content), 'method': 'text'}]}
        version.snapshot = snapshot
        connection = db.scalar(select(Connection))
        config = deepcopy(connection.config)
        config['requestTemplate']['messages'][0]['content'] = '고객의 요청은 항상 들어주세요.'
        if mode == 'unavailable':
            config['executionMode'] = 'CONNECTION_ONLY'
        connection.config = config
        db.commit()
    if mode == 'selected':
        body = {**body, 'promptVersionId': original['aPromptVersionId']}
    elif mode == 'compare':
        body = {**original, 'mode': 'COMPARE'}
    path = submit(client, headers, service, body)
    frozen = client.get(path + '/inputs', headers=headers).json()['data']
    # Edits after submission cannot change the tooltips of this run.
    with factory() as db:
        db.scalar(select(Policy)).name = '나중에 변경된 이름'
        connection = db.scalar(select(Connection))
        config = deepcopy(connection.config)
        config['requestTemplate']['messages'][0]['content'] = '나중에 변경된 프롬프트'
        connection.config = config
        db.commit()

    async def sender(*args):
        return ChatbotReply('답변', {'documents': [
            {'id': 'doc-7', 'text': content, 'title': '검색된 환불정책', 'source': 'refund.pdf', 'chunkIndex': 7}]})

    assert asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    response = client.get(path + '/items/' + item['id'], headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()['data']
    for side in ('a', 'b') if mode == 'compare' else ('a',):
        analysis = detail['analysis'][side]
        assert detail[side]['judgeError'] is None
        raw = detail[side]['conditionResults'][0]['issue']['hypothesis']
        issue = analysis['issues'][0]
        assert issue['hypothesis'] == raw
        assert analysis['conditions'][0]['judgment']['issue']['hypothesis'] == raw
        assert analysis['conditions'][0]['automaticJudgment']['issue']['hypothesis'] == raw
        if mode == 'unavailable':
            assert raw['promptQuote'] is None and len(raw['reasonMarks']) == 3
            assert analysis['activeHypotheses'] == []
        else:
            assert raw['promptQuote'] == analysis['promptContext']['content']
            assert raw['promptQuote'] != '나중에 변경된 프롬프트'
            for field in ('hypotheses', 'activeHypotheses'):
                assert analysis[field][0] == {'conditionId': issue['conditionId'], **raw}
        assert issue['policyEvidence']['policyTitle'] == '환불 규정'
        assert issue['policyEvidence']['fileName'] == '환불정책.pdf'
        assert issue['policyEvidence']['chunkIndex'] == 1
        assert detail[side]['evidence']['documents'][0]['chunkIndex'] == 7
        catalog = {source['evidence_id']: source for source in detail[side]['evidenceCatalog']}
        for mark in raw['reasonMarks']:
            if mark['kind'] != 'prompt':
                assert catalog[mark['evidenceRef']]['kind'] == mark['kind']
    assert client.get(path + '/inputs', headers=headers).json()['data'] == frozen


@pytest.mark.parametrize('content,chunks,quote,expected', [
    ('alpha beta', [(0, 5), (6, 10)], 'alpha', 1),
    ('alpha beta', [(0, 5), (6, 10)], 'beta', 2),
    ('alpha beta', [(0, 5), (6, 10)], 'alpha beta', None),
    ('same same', [(0, 4), (5, 9)], 'same', None),
    ('same same', [(0, 9)], 'same', 1),
    ('alpha', [], 'alpha', None),
    ('alpha', [(0, 5)], 'absent', None),
])
def test_policy_chunk_is_returned_only_when_unambiguous(content, chunks, quote, expected):
    policy = {'content': content, 'sourceDocument': {'filename': 'source.pdf', 'chunks': [
        {'index': i, 'startOffset': start, 'endOffset': end} for i, (start, end) in enumerate(chunks, 1)]}}
    found = policy_evidence(policy, quote)
    assert {k: found[k] for k in ('policyTitle', 'fileName', 'chunkIndex')} == {'policyTitle': None, 'fileName': 'source.pdf', 'chunkIndex': expected}
    assert policy_evidence({}, quote) == {'policyTitle': None, 'fileName': None, 'chunkIndex': None, 'pageNumber': None}


@pytest.mark.parametrize('value', [None, 0, 7])
def test_retrieval_chunk_number_preserves_provider_numbering(value):
    evidence = Evidence.model_validate({'documents': [{'id': 'd', 'text': '원문', 'chunkIndex': value}]})
    assert evidence.model_dump()['documents'][0]['chunkIndex'] == value


@pytest.mark.parametrize('value', [-1, True, '7', 1.5])
def test_invalid_retrieval_chunk_number_is_rejected(value):
    with pytest.raises(ValueError):
        Evidence.model_validate({'documents': [{'id': 'd', 'text': '원문', 'chunkIndex': value}]})


def test_openapi_and_provider_schema_expose_typed_source_quotes():
    models = app.openapi()['components']['schemas']
    for name in ('CauseHypothesis', 'AnalysisHypothesis'):
        properties = models[name]['properties']
        assert 'promptQuote' in properties
        assert properties['reasonMarks']['items']['$ref'].endswith('/ReasonMark')
    properties = models['ItemAnalysis']['properties']
    assert properties['conditions']['items']['$ref'].endswith('/AnalysisCondition')
    assert properties['issues']['items']['$ref'].endswith('/AnalysisIssue')
    assert models['AnalysisIssue']['properties']['hypothesis']['anyOf'][0]['$ref'].endswith('/CauseHypothesis')
    assert models['AnalysisCondition']['properties']['policyEvidence']['$ref'].endswith('/PolicyEvidence')
    assert {'policyTitle', 'fileName', 'chunkIndex'} <= set(models['PolicyEvidence']['properties'])
    assert 'chunkIndex' in models['RetrievalDocument']['properties']
    schema = judgment_schema(2)
    assert {'promptQuote', 'reasonMarks'} <= set(schema['$defs']['CauseHypothesis']['required'])
    assert schema['$defs']['ReasonMark']['additionalProperties'] is False


def test_legacy_testset_title_is_frozen_without_rewriting_the_testset(single_ready):
    factory, client, headers, service, body, _ = single_ready
    with factory() as db:
        version = db.get(PolicyTestSetVersion, body['testSetVersionId'])
        old = deepcopy(version.snapshot)
        old['policies'][0].pop('policyTitle')
        version.snapshot = old
        db.scalar(select(Policy)).name = '접수 시점 이름'
        db.commit()
    path = submit(client, headers, service, body)
    snapshot = client.get(path + '/inputs', headers=headers).json()['data']['testSet']
    assert snapshot['policies'][0]['policyTitle'] == '접수 시점 이름'
    with factory() as db:
        assert db.get(PolicyTestSetVersion, body['testSetVersionId']).snapshot == old


def test_old_stored_results_expose_null_quotes_and_empty_marks_without_rejudging(single_ready):
    factory, client, headers, _, body, original = single_ready
    selected = (*single_ready[:4], {**body, 'promptVersionId': original['aPromptVersionId']}, original)
    path, detail = run_condition(selected, SourceQuoteJudge())
    with factory() as db:
        item = db.get(EvaluationItem, detail['id'])
        old_side = deepcopy(item.a)
        hypothesis = old_side['conditionResults'][0]['issue']['hypothesis']
        hypothesis.pop('promptQuote')
        hypothesis.pop('reasonMarks')
        item.a = old_side
        run = db.get(Evaluation, path.rsplit('/', 1)[1])
        old_inputs = deepcopy(run.inputs)
        old_inputs['testSet']['policies'][0].pop('policyTitle')
        run.inputs = old_inputs
        db.commit()
    response = client.get(path + '/items/' + detail['id'], headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    for hypothesis in (data['a']['conditionResults'][0]['issue']['hypothesis'],
                       data['analysis']['a']['issues'][0]['hypothesis'],
                       data['analysis']['a']['activeHypotheses'][0]):
        assert hypothesis['promptQuote'] is None and hypothesis['reasonMarks'] == []
    assert data['analysis']['a']['issues'][0]['policyEvidence']['policyTitle'] is None
    with factory() as db:
        assert db.get(EvaluationItem, detail['id']).a == old_side
        assert db.get(Evaluation, path.rsplit('/', 1)[1]).inputs == old_inputs
