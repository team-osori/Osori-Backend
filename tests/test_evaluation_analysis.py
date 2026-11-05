from copy import deepcopy

import pytest

from app.services.evaluation_analysis import describe_side
from test_condition_evaluation import ConditionJudge, run_condition
from test_single_evaluation import single_ready  # noqa: F401


class FailingConditionJudge(ConditionJudge):
    async def judge(self, **kwargs):
        output = await super().judge(**kwargs)
        output['policy']['verdict'] = 'fail'
        condition = output['conditionResults'][0]
        condition['verdict'] = 'fail'
        condition['reason'] = '취소 기한 조건을 누락했습니다.'
        condition['issue'] = {'kind': 'condition_omitted', 'answerQuote': None,
                              'missingGuidance': ['취소 기한 안내'], 'recommendation': '취소 기한을 명시하세요.'}
        return output


def test_issue_connects_answer_condition_policy_and_recommendation(single_ready):
    path, detail = run_condition(single_ready, FailingConditionJudge())
    factory, client, headers, *_ = single_ready
    issue = detail['analysis']['a']['issues'][0]
    assert issue['conditionId'] == detail['input']['expectedConditions'][0]['id']
    assert issue['policyEvidence']['quote'] == detail['input']['evidenceQuote']
    assert issue['recommendation'] == '취소 기한을 명시하세요.'
    assert detail['resultStatus'] == 'UNMET'
    summary = client.get(path + '/summary', headers=headers).json()['data']['analysis']
    assert summary['variants']['a']['statuses']['UNMET'] == 1
    assert summary['variants']['a']['issueTypes'] == {'condition_omitted': 1}
    assert len(client.get(path + '/items', headers=headers, params={'resultStatus': 'UNMET', 'issueKind': 'condition_omitted'}).json()['data']['items']) == 1
    assert client.get(path + '/items', headers=headers, params={'resultStatus': 'MET'}).json()['data']['items'] == []


@pytest.mark.parametrize('fault', ['missing_issue', 'invented_quote'])
def test_error_analysis_requires_real_evidence(single_ready, fault):
    class InvalidFindingJudge(FailingConditionJudge):
        async def judge(self, **kwargs):
            output = await super().judge(**kwargs)
            issue = output['conditionResults'][0]['issue']
            if fault == 'missing_issue':
                output['conditionResults'][0]['issue'] = None
            else:
                issue['answerQuote'] = '답변에 없는 문장'
            return output
    path, detail = run_condition(single_ready, InvalidFindingJudge())
    assert detail['analysis']['a']['status'] == 'JUDGMENT_ERROR'
    assert detail['analysis']['a']['issues'] == []
    assert detail['a']['judgeError']['code'] == 'INVALID_JUDGE_OUTPUT'


def test_mixed_failure_and_unavailable_results_preserve_both_signals():
    side = {'callStatus': 'SUCCEEDED', 'criteria': {
        'policy': {'status': 'judged', 'verdict': 'fail'},
        'questionResolution': {'status': 'error', 'verdict': None}}}
    result = describe_side({}, deepcopy(side))
    assert result['status'] == 'UNMET' and result['hasJudgmentError'] and result['needsReview']


def test_failed_condition_is_visible_even_when_all_category_summaries_pass():
    """Individual expectations remain authoritative for the overall item status."""
    side = {'callStatus': 'SUCCEEDED', 'criteria': {'policy': {'status': 'judged', 'verdict': 'pass'}},
            'conditionResults': [{'conditionId': 'c', 'status': 'judged', 'verdict': 'fail'}]}
    assert describe_side({}, side)['status'] == 'UNMET'
