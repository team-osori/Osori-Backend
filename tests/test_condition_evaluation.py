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
    assert condition['id'].startswith('legacy-')
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
    snapshot = {'policies': [{'id': 'p', 'content': '정책 원문'}], 'questions': [
        {'id': 'q', 'expectedBehavior': '기대 행동', 'policyVersionId': 'p', 'evidenceQuote': '정책 원문'}]}
    prepared = prepare_questions(snapshot)
    assert 'expectedConditions' not in snapshot['questions'][0]
    duplicate = deepcopy(prepared)
    duplicate['questions'][0]['expectedConditions'] *= 2
    with pytest.raises(ValueError):
        prepare_questions(duplicate)
    prepared['questions'][0]['expectedConditions'][0]['sourceQuote'] = '없는 근거'
    with pytest.raises(ValueError):
        prepare_questions(prepared)
