"""Join failed answers to frozen expectations and source evidence for the product UI."""
from collections import Counter
from copy import deepcopy

from app.services.evaluations import side_names

STATUSES = ('MET', 'UNMET', 'REVIEW_REQUIRED', 'JUDGMENT_ERROR', 'EXECUTION_ERROR', 'NOT_RUN', 'IN_PROGRESS')


def apply_reviews(question, side, reviews):
    effective = deepcopy(side)
    conditions = {condition['id']: condition for condition in question.get('expectedConditions', [])}
    results = {result['conditionId']: result for result in effective.get('conditionResults', [])}
    categories = set()
    for review in reviews:
        condition = conditions[review['conditionId']]
        categories.add(condition['criterion'])
        result = results.get(condition['id'], {'question_id': question['id'], 'conditionId': condition['id'],
                    'evidence_refs': [], 'error': None})
        result.update(status='not_evaluable' if review['verdict'] == 'not_evaluable' else 'judged',
                      verdict=None if review['verdict'] == 'not_evaluable' else review['verdict'],
                      reason=review['reason'], humanReview=review, issue=None)
        if review['verdict'] == 'fail':
            result['issue'] = {'kind': 'other', 'answerQuote': None, 'missingGuidance': [],
                               'recommendation': None, 'hypothesis': None}
        results[condition['id']] = result
    effective['conditionResults'] = list(results.values())
    for category in categories:
        related = [results.get(condition['id'], {}) for condition in conditions.values() if condition['criterion'] == category]
        verdict = 'fail' if any(result.get('verdict') == 'fail' for result in related) else (
            'pass' if all(result.get('verdict') == 'pass' for result in related) else None)
        effective['criteria'][category] = {'status': 'judged' if verdict else 'not_evaluable', 'verdict': verdict}
    return effective


def describe_side(question, side, reviews=()):
    automatic = side
    if side and reviews:
        side = apply_reviews(question, side, reviews)
    if not side or side['callStatus'] == 'NOT_RUN':
        status = 'NOT_RUN'
    elif side['callStatus'] == 'RUNNING':
        status = 'IN_PROGRESS'
    elif side['callStatus'] != 'SUCCEEDED':
        status = 'EXECUTION_ERROR'
    else:
        results = list(side['criteria'].values()) + side.get('conditionResults', [])
        if any(result['verdict'] == 'fail' for result in results):
            status = 'UNMET'
        elif any(result['status'] == 'error' for result in results):
            status = 'JUDGMENT_ERROR'
        elif any(result['status'] != 'judged' for result in results):
            status = 'REVIEW_REQUIRED'
        else:
            status = 'MET'
    decisions = {result['conditionId']: result for result in (side or {}).get('conditionResults', [])}
    conditions = []
    issues = []
    for condition in question.get('expectedConditions', []):
        decision = decisions.get(condition['id'])
        record = {'conditionId': condition['id'], 'criterion': condition['criterion'],
                  'requirement': condition['requirement'],
                  'policyEvidence': {'policyVersionId': condition['policyVersionId'], 'quote': condition['sourceQuote']},
                  'judgment': decision,
                  'automaticJudgment': next((result for result in (automatic or {}).get('conditionResults', [])
                                             if result['conditionId'] == condition['id']), None)}
        conditions.append(record)
        if decision and decision['verdict'] == 'fail' and decision.get('issue'):
            issues.append({**record, **decision['issue'], 'reason': decision['reason']})
    results = list((side or {}).get('criteria', {}).values()) + (side or {}).get('conditionResults', [])
    errors = [result for result in results if result['status'] == 'error']
    unavailable = [result for result in results if result['status'] == 'not_evaluable']
    return {'status': status, 'needsReview': status not in ('MET', 'NOT_RUN', 'IN_PROGRESS') or bool(errors or unavailable),
            'hasJudgmentError': bool(errors), 'hasUnevaluableCriteria': bool(unavailable),
            'conditions': conditions, 'issues': issues, 'humanReviewApplied': bool(reviews)}


def item_analysis(run, item, reviews=None):
    reviews = reviews or {}
    return {name: {**describe_side(item.input, getattr(item, name),
                    [value for (item_id, side, condition_id), value in reviews.items() if item_id == item.id and side == name]),
                   **cause_analysis(run.inputs, item.input, name, getattr(item, name))}
            for name in side_names(run.inputs)}


def cause_analysis(inputs, question, name, side):
    """Keep observable input/output facts separate from unproven causal attribution."""
    documents = (side or {}).get('evidence', {}).get('documents')
    prompt = inputs.get('prompts', {}).get(name)
    facts = {'retrievalTelemetryAvailable': documents is not None,
             'retrievedDocumentCount': len(documents) if documents is not None else None,
             'selectedPromptVersionId': prompt['id'] if prompt else None,
             'promptApplicationVerified': inputs.get('comparisonAssurance', {}).get('promptAppliedVerified', False),
             'conditionSources': []}
    for condition in question.get('expectedConditions', []):
        matches = None if documents is None else [doc['id'] for doc in documents if condition['sourceQuote'] in doc['text']]
        facts['conditionSources'].append({'conditionId': condition['id'],
                                         'documentsContainingExactQuote': matches})
    limitations = ['CAUSAL_ISOLATION_NOT_PERFORMED', 'EXTERNAL_MODEL_SETTINGS_UNVERIFIED']
    if documents is None:
        limitations.append('RETRIEVAL_TELEMETRY_UNAVAILABLE')
    else:
        limitations.append('EXACT_QUOTE_MATCH_IS_NOT_SEMANTIC_RETRIEVAL_EVALUATION')
    if not prompt:
        limitations.append('PROMPT_INTERNALS_UNAVAILABLE')
    elif not facts['promptApplicationVerified']:
        limitations.append('PROMPT_APPLICATION_UNVERIFIED')
    hypotheses = []
    for decision in (side or {}).get('conditionResults', []):
        hypothesis = (decision.get('issue') or {}).get('hypothesis')
        if not hypothesis:
            continue
        if hypothesis['area'] == 'RETRIEVAL' and documents is None:
            continue
        if hypothesis['area'] == 'PROMPT' and not prompt:
            continue
        hypotheses.append({'conditionId': decision['conditionId'], **hypothesis})
    return {'causeStatus': 'HYPOTHESIS' if hypotheses else 'UNDETERMINED',
            'confirmedCause': None, 'facts': facts, 'hypotheses': hypotheses, 'limitations': limitations}


def summarize(run, items, reviews=None):
    result = {}
    for name in side_names(run.inputs):
        statuses = Counter()
        kinds = Counter()
        review = 0
        for item in items:
            analysis = item_analysis(run, item, reviews)[name]
            statuses[analysis['status']] += 1
            review += analysis['needsReview']
            kinds.update(issue['kind'] for issue in analysis['issues'])
        result[name] = {'statuses': {key: statuses[key] for key in STATUSES},
                        'needsReviewCount': review, 'issueTypes': dict(kinds)}
    return {'variants': result, 'issueCountsIncludeMultipleIssuesPerQuestion': True,
            'basis': 'LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC', 'criterionPassRatesBasis': 'AUTOMATIC'}
