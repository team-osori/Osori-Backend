import asyncio
from copy import deepcopy

import pytest

from app import evaluation_worker as worker
from app.main import app
from app.services.evaluation_judge import get_evaluation_judge
from app.services.condition_evaluation import prepare_questions
from test_evaluations import submit
from test_single_evaluation import single_ready  # noqa: F401


class ConditionJudge:
    def descriptor(self):
        return {'adapter': 'condition-test', 'provider': 'test', 'model': 'test'}

    async def judge(self, *, question, evidence_catalog, criteria, **kwargs):
        refs = [{'evidence_id': source['evidence_id'], 'explanation': '원문 확인'}
                for source in evidence_catalog if source['kind'] in ('answer', 'policy')]
        result = {'question_id': question['id'], 'status': 'judged', 'verdict': 'pass',
                  'reason': '조건 충족', 'evidence_refs': refs, 'error': None}
        return {'policy': deepcopy(result), 'questionResolution': {**result, 'score': 4},
                'communicationQuality': {**result, 'score': 3},
                'conditionResults': [{**result, 'conditionId': condition['id']} for condition in question['expectedConditions']]}


async def answer(*args):
    return '출발 24시간 전까지 취소 가능합니다.'


def run_condition(single_ready, judge):
    factory, client, headers, service, body, original = single_ready
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
    return path, detail


def test_condition_judgements_are_linked_to_frozen_policy_and_question(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    inputs = client.get(path + '/inputs', headers=headers).json()['data']
    condition = inputs['testSet']['questions'][0]['expectedConditions'][0]
    assert condition['id']
    assert condition['kind'] == 'required'
    assert detail['a']['conditionResults'][0]['conditionId'] == condition['id']
    assert detail['a']['criteria']['questionResolution']['score'] == 4
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert set(summary['criteria']) == {'policy', 'questionResolution', 'communicationQuality'}
    assert summary['criteria']['policy']['a']['passRate'] == 100


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'foreign', 'score', 'question', 'evidence', 'policy_summary'])
def test_malformed_condition_output_is_a_judge_error(single_ready, fault):
    class InvalidJudge(ConditionJudge):
        async def judge(self, **kwargs):
            result = await super().judge(**kwargs)
            condition = result['conditionResults'][0]
            if fault == 'missing':
                result['conditionResults'] = []
            elif fault == 'duplicate':
                result['conditionResults'].append(deepcopy(condition))
            elif fault == 'foreign':
                condition['conditionId'] = 'foreign'
            elif fault == 'score':
                result['questionResolution']['score'] = 1
            elif fault == 'question':
                condition['question_id'] = 'foreign'
            elif fault == 'evidence':
                condition['evidence_refs'] = [ref for ref in condition['evidence_refs'] if ref['evidence_id'] != 'ev_001']
            elif fault == 'policy_summary':
                result['policy']['verdict'] = 'fail'
            return result
    path, detail = run_condition(single_ready, InvalidJudge())
    assert detail['a']['judgeError']['code'] == 'INVALID_JUDGE_OUTPUT'
    assert all(result['status'] == 'error' for result in detail['a']['criteria'].values())
    factory, client, headers, *_ = single_ready
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['criteria']['policy']['a']['denominator'] == 0


def test_invalid_expected_sources_and_duplicate_ids_are_rejected():
    snapshot = {'policies': [{'id': 'p', 'content': '정책 원문', 'rules': [{'key': 'r'}]}], 'questions': [
        {'id': 'q', 'expectedBehavior': '기대 행동', 'policyVersionId': 'p', 'evidenceQuote': '정책 원문', 'ruleKeys': ['r'], 'forbiddenBehaviors': []}]}
    prepared = prepare_questions(snapshot)
    assert 'expectedConditions' not in snapshot['questions'][0]
    duplicate = deepcopy(prepared)
    duplicate['questions'][0]['expectedConditions'] *= 2
    with pytest.raises(ValueError):
        prepare_questions(duplicate)
    prepared['questions'][0]['expectedConditions'][0]['sourceQuote'] = '없는 근거'
    with pytest.raises(ValueError):
        prepare_questions(prepared)


def test_a_confirmed_conditions_keep_identifiers_kind_and_source(single_ready):
    from app.models.policy import TestSetVersion
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, service, body, original = single_ready
    with factory() as db:
        source = deepcopy(db.get(TestSetVersion, body['testSetVersionId']).snapshot)
    frozen = client.get(path + '/inputs', headers=headers).json()['data']['testSet']
    before = source['questions'][0]['expectedConditions']
    after = frozen['questions'][0]['expectedConditions']
    assert [c['id'] for c in after] == [c['id'] for c in before]
    assert {c['kind'] for c in after} == {'required', 'forbidden'}
    assert [c['requirement'] for c in after] == [c['description'] for c in before]
    assert [c['sourceQuote'] for c in after] == [c['evidenceQuote'] for c in before]
    assert [c['ruleKeys'] for c in after] == [c['ruleKeys'] for c in before]
    with factory() as db:
        assert db.get(TestSetVersion, body['testSetVersionId']).snapshot == source


def test_a_display_categories_preserve_diagnostic_conditions():
    policy = {'id': 'p', 'content': '정책 원문', 'rules': [{'key': 'r'}]}
    question = {'id': 'q', 'expectedBehavior': '안내', 'forbiddenBehaviors': [],
                'policyVersionId': 'p', 'ruleKeys': ['r'], 'evidenceQuote': '정책 원문',
                'expectedConditions': [{'id': category, 'description': '조건 확인', 'kind': 'required',
                    'category': category, 'policyVersionId': 'p', 'ruleKeys': ['r'], 'evidenceQuote': '정책 원문'}
                    for category in ('policy', 'response', 'grounding', 'retrieval')]}
    normalized = prepare_questions({'policies': [policy], 'questions': [question]})['questions'][0]['expectedConditions']
    assert [c['category'] for c in normalized] == ['policy', 'response', 'grounding', 'retrieval']
    assert [c['criterion'] for c in normalized] == ['policy', 'questionResolution', 'grounding', 'retrieval']
