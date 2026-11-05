from copy import deepcopy
from types import SimpleNamespace

from app.services.evaluation_analysis import cause_analysis
from test_condition_evaluation import run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401


def test_missing_retrieval_never_confirms_a_search_or_model_cause(single_ready):
    path, detail = run_condition(single_ready, FailingConditionJudge())
    analysis = detail['analysis']['a']
    assert analysis['causeStatus'] == 'UNDETERMINED'
    assert analysis['confirmedCause'] is None
    assert analysis['facts']['retrievedDocumentCount'] is None
    assert 'RETRIEVAL_TELEMETRY_UNAVAILABLE' in analysis['limitations']
    assert 'PROMPT_INTERNALS_UNAVAILABLE' in analysis['limitations']
    assert analysis['issues'][0]['policyEvidence']['quote']


def test_observable_exact_quote_and_suspected_causes_remain_distinct():
    condition = {'id': 'c', 'sourceQuote': '정책 근거'}
    hypothesis = {'area': 'RETRIEVAL', 'reason': '관련 근거가 부족할 가능성',
                  'evidenceRefs': [{'evidence_id': 'ev_001', 'explanation': '답변 확인'}]}
    side = {'evidence': {'documents': [{'id': 'doc1', 'text': '정책 근거 원문'}]},
            'conditionResults': [{'conditionId': 'c', 'issue': {'hypothesis': hypothesis}}]}
    result = cause_analysis({'prompts': {}}, {'expectedConditions': [condition]}, 'a', side)
    assert result['facts']['conditionSources'][0]['documentsContainingExactQuote'] == ['doc1']
    assert result['causeStatus'] == 'HYPOTHESIS' and result['confirmedCause'] is None
    missing = deepcopy(side)
    missing['evidence']['documents'] = None
    assert cause_analysis({'prompts': {}}, {'expectedConditions': [condition]}, 'a', missing)['hypotheses'] == []
    empty = deepcopy(side)
    empty['evidence']['documents'] = []
    assert cause_analysis({'prompts': {}}, {'expectedConditions': [condition]}, 'a', empty)['facts']['conditionSources'][0]['documentsContainingExactQuote'] == []


def test_uncollected_hypothesis_evidence_is_rejected(single_ready):
    class InvalidCauseJudge(FailingConditionJudge):
        async def judge(self, **kwargs):
            output = await super().judge(**kwargs)
            output['conditionResults'][0]['issue']['hypothesis'] = {
                'area': 'MODEL', 'reason': '모델 오류 가능성',
                'evidenceRefs': [{'evidence_id': 'invented', 'explanation': '없는 근거'}]}
            return output
    path, detail = run_condition(single_ready, InvalidCauseJudge())
    assert detail['a']['judgeError']['code'] == 'INVALID_JUDGE_OUTPUT'
    assert detail['analysis']['a']['hypotheses'] == []
