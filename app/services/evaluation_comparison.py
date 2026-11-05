"""Compare identified conditions only under the same frozen question and grading contract."""
from collections import Counter

from app.core.errors import ApiError
from app.services.evaluation_analysis import describe_side
from app.services.evaluations import TERMINAL

LABELS = ('RESOLVED', 'REMAINING', 'NEW', 'UNCHANGED_PASS', 'NOT_COMPARABLE')


def validate_comparison(baseline, target):
    if baseline.id == target.id:
        raise ApiError(400, 'SAME_EVALUATION', '서로 다른 평가를 선택하세요.')
    if baseline.status not in TERMINAL or target.status not in TERMINAL:
        raise ApiError(409, 'EVALUATION_NOT_TERMINAL', '평가 종료 후 비교할 수 있습니다.')
    if baseline.inputs.get('mode') != 'SINGLE' or target.inputs.get('mode') != 'SINGLE':
        raise ApiError(409, 'SINGLE_EVALUATION_REQUIRED', '단일 대상 평가끼리 비교하세요.')
    if any(run.inputs.get('criteria', {}).get('version') != 2 for run in (baseline, target)):
        raise ApiError(409, 'UNSUPPORTED_EVALUATION_PROFILE', '조건별 판정을 지원하는 v2 평가끼리 비교하세요.')
    if any(baseline.inputs[key] != target.inputs[key] for key in ('testSet', 'criteria', 'judge')):
        raise ApiError(409, 'COMPARISON_INPUT_MISMATCH', '질문·기대 조건·정책·판정 기준이 동일해야 합니다.')
    if baseline.inputs['connection']['config'] != target.inputs['connection']['config']:
        raise ApiError(409, 'COMPARISON_CONFIGURATION_CHANGED', '기업 API 연동 설정이 다릅니다.')


def comparable_decisions(run, item, reviews):
    if not item or not item.a or item.a['callStatus'] != 'SUCCEEDED':
        return {}
    relevant = [value for (item_id, side, condition_id), value in reviews.items() if item_id == item.id and side == 'a']
    analysis = describe_side(item.input, item.a, relevant)
    return {condition['conditionId']: condition['judgment'] for condition in analysis['conditions']}


def compare(baseline, target, baseline_items, target_items, reviews):
    validate_comparison(baseline, target)
    before = {item.question_id: item for item in baseline_items}
    after = {item.question_id: item for item in target_items}
    items = []
    counts = Counter()
    for question in baseline.inputs['testSet']['questions']:
        left_item, right_item = before.get(question['id']), after.get(question['id'])
        left = comparable_decisions(baseline, left_item, reviews)
        right = comparable_decisions(target, right_item, reviews)
        changes = []
        for condition in question['expectedConditions']:
            a, b = left.get(condition['id']), right.get(condition['id'])
            pair = ((a or {}).get('verdict'), (b or {}).get('verdict'))
            if not a or not b or a['status'] != 'judged' or b['status'] != 'judged':
                label = 'NOT_COMPARABLE'
            else:
                label = {('fail', 'pass'): 'RESOLVED', ('fail', 'fail'): 'REMAINING',
                         ('pass', 'fail'): 'NEW', ('pass', 'pass'): 'UNCHANGED_PASS'}.get(pair, 'NOT_COMPARABLE')
            counts[label] += 1
            changes.append({'conditionId': condition['id'], 'criterion': condition['criterion'],
                            'change': label, 'before': pair[0], 'after': pair[1]})
        items.append({'questionId': question['id'], 'baselineItemId': left_item.id if left_item else None,
                      'targetItemId': right_item.id if right_item else None, 'conditions': changes,
                      'baselineReviewRevision': left_item.review_revision if left_item else None,
                      'targetReviewRevision': right_item.review_revision if right_item else None})
    if counts['RESOLVED'] and counts['NEW']:
        outcome = 'MIXED'
    elif counts['NEW']:
        outcome = 'REGRESSED'
    elif counts['NOT_COMPARABLE']:
        outcome = 'INCOMPLETE'
    elif counts['RESOLVED']:
        outcome = 'IMPROVED'
    else:
        outcome = 'NO_IMPROVEMENT'
    return {'baselineEvaluationId': baseline.id, 'targetEvaluationId': target.id, 'outcome': outcome,
            'counts': {label: counts[label] for label in LABELS}, 'countUnit': 'CONDITION',
            'hasNewIssues': bool(counts['NEW']), 'hasIncompleteComparison': bool(counts['NOT_COMPARABLE']),
            'basis': 'LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC',
            'externalEnvironmentControlled': False, 'items': items}
