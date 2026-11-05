"""Join failed answers to frozen expectations and source evidence for the product UI."""
from collections import Counter
from copy import deepcopy

from app.services.evaluations import side_names
from app.services.condition_evaluation import normalize_condition
from app.services.evaluation_sources import policy_evidence, prompt_context

SCREEN_COUNTS = {'CORRECT': 'correct', 'INCORRECT': 'incorrect', 'PENDING': 'pending',
                 'REMAINING': 'remaining', 'EXECUTION_ERROR': 'executionError',
                 'JUDGMENT_ERROR': 'judgmentError', 'REVIEW_REQUIRED': 'reviewRequired'}
CAUSE_AREAS = ('RETRIEVAL', 'PROMPT', 'MODEL')
ANALYSIS_STATUSES = ('HYPOTHESIS_AVAILABLE', 'UNDETERMINED', 'UNAVAILABLE', 'NOT_APPLICABLE')

STATUSES = ('MET', 'UNMET', 'REVIEW_REQUIRED', 'JUDGMENT_ERROR', 'EXECUTION_ERROR', 'NOT_RUN', 'IN_PROGRESS')


def apply_reviews(question, side, reviews):
    effective = deepcopy(side)
    conditions = {condition['id']: normalize_condition(condition)
                  for condition in question.get('expectedConditions', [])}
    results = {result['conditionId']: result for result in effective.get('conditionResults', [])}
    categories = set()
    for review in reviews:
        condition = conditions[review['conditionId']]
        categories.add(condition['criterion'])
        result = results.get(condition['id'], {'question_id': question['id'], 'conditionId': condition['id'],
                    'evidence_refs': [], 'error': None})
        result.update(status='not_evaluable' if review['verdict'] == 'not_evaluable' else 'judged',
                      verdict=None if review['verdict'] == 'not_evaluable' else review['verdict'],
                      reason=review['reason'], humanReview=review, issue=None, error=None)
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


def describe_side(question, side, reviews=(), *, expected_criteria=None):
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
    for source_condition in question.get('expectedConditions', []):
        condition = normalize_condition(source_condition)
        decision = decisions.get(condition['id'])
        record = {'conditionId': condition['id'], 'criterion': condition['criterion'],
                  'kind': condition['kind'], 'category': condition['category'], 'ruleKeys': condition['ruleKeys'],
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
    screen = screen_status(question, side, expected_criteria=expected_criteria)
    return {'status': status, 'screenStatus': screen, 'needsReview': screen in ('EXECUTION_ERROR', 'JUDGMENT_ERROR', 'REVIEW_REQUIRED', 'INCORRECT'),
            'hasJudgmentError': bool(errors), 'hasUnevaluableCriteria': bool(unavailable),
            'conditions': conditions, 'issues': issues, 'humanReviewApplied': bool(reviews)}


def screen_status(question, side, *, expected_criteria=None):
    """Exclusive UI buckets: incomplete judgement never becomes an O/X grade."""
    call = (side or {}).get('callStatus', 'NOT_RUN')
    if call == 'NOT_RUN':
        return 'REMAINING'
    if call == 'RUNNING':
        return 'PENDING'
    if call != 'SUCCEEDED':
        return 'EXECUTION_ERROR'
    results = list(side.get('criteria', {}).values()) + side.get('conditionResults', [])
    if any(result['status'] == 'error' for result in results):
        return 'JUDGMENT_ERROR'
    if expected_criteria and not set(expected_criteria) <= set(side.get('criteria', {})):
        return 'REVIEW_REQUIRED'
    decisions = {result['conditionId'] for result in side.get('conditionResults', [])}
    if (not results or any(result['status'] != 'judged' or result['verdict'] is None for result in results)
            or any(condition['id'] not in decisions for condition in question.get('expectedConditions', []))):
        return 'REVIEW_REQUIRED'
    return 'INCORRECT' if any(result['verdict'] == 'fail' for result in results) else 'CORRECT'


def reviewed_side(question, side, reviews=(), overall=None, *, expected_criteria=None):
    """One O/X projection shared by analytics and the Figma grading grid."""
    detail = describe_side(question, side, reviews, expected_criteria=expected_criteria)
    base_screen_status = detail['screenStatus']
    conflict = bool(overall and base_screen_status in ('CORRECT', 'INCORRECT')
                    and overall['verdict'] in ('pass', 'fail')
                    and (overall['verdict'] == 'pass') != (base_screen_status == 'CORRECT'))
    if conflict or (overall and overall['verdict'] == 'not_evaluable'
                    and base_screen_status in ('CORRECT', 'INCORRECT')):
        detail['screenStatus'] = 'REVIEW_REQUIRED'
        detail['needsReview'] = True
    return {**detail, 'answerReview': overall, 'reviewConflict': conflict, 'derivedScreenStatus': base_screen_status}


def item_analysis(run, item, reviews=None, answer_reviews=None):
    reviews = reviews or {}
    result = {}
    for name in side_names(run.inputs):
        overall = (answer_reviews or {}).get((item.id, name))
        detail = reviewed_side(item.input, getattr(item, name),
            [value for (item_id, side, condition_id), value in reviews.items() if item_id == item.id and side == name],
            overall, expected_criteria=run.inputs.get('criteria', {}).get('definitions'))
        policies = {policy['id']: policy for policy in run.inputs.get('testSet', {}).get('policies', [])}
        for condition in detail['conditions']:
            source = condition['policyEvidence']
            source.update(policy_evidence(policies.get(source['policyVersionId'], {}), source['quote']))
        causes = cause_analysis(run.inputs, item.input, name, getattr(item, name))
        failed = {condition['conditionId'] for condition in detail['conditions']
                  if condition['judgment'] and condition['judgment']['verdict'] == 'fail'}
        active = [hypothesis for hypothesis in causes['hypotheses'] if hypothesis['conditionId'] in failed
                  and detail['screenStatus'] == 'INCORRECT']
        analysis_status = ('NOT_APPLICABLE' if detail['screenStatus'] == 'CORRECT' else
                           'UNAVAILABLE' if detail['screenStatus'] != 'INCORRECT' else
                           'HYPOTHESIS_AVAILABLE' if active else 'UNDETERMINED')
        result[name] = {**detail, **causes, 'activeHypotheses': active,
                        'activeHypothesisAreas': sorted({hypothesis['area'] for hypothesis in active}),
                        'analysisStatus': analysis_status,
                        'hypothesesBasis': 'AUTOMATIC',
                        'activeHypothesesBasis': 'LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC',
                        'promptContext': prompt_context(run.inputs, name)}
    return result


def cause_analysis(inputs, question, name, side):
    """Keep observable input/output facts separate from unproven causal attribution."""
    documents = (side or {}).get('evidence', {}).get('documents')
    prompt = inputs.get('prompts', {}).get(name)
    context = prompt_context(inputs, name)
    facts = {'retrievalTelemetryAvailable': documents is not None,
             'retrievedDocumentCount': len(documents) if documents is not None else None,
             'selectedPromptVersionId': prompt['id'] if prompt else None,
             'promptApplicationVerified': inputs.get('comparisonAssurance', {}).get('promptAppliedVerified', False),
             'conditionSources': []}
    for condition in question.get('expectedConditions', []):
        quote = condition['sourceQuote'] if 'sourceQuote' in condition else condition['evidenceQuote']
        matches = None if documents is None else [doc['id'] for doc in documents if quote in doc['text']]
        facts['conditionSources'].append({'conditionId': condition['id'],
                                         'documentsContainingExactQuote': matches})
    limitations = ['CAUSAL_ISOLATION_NOT_PERFORMED', 'EXTERNAL_MODEL_SETTINGS_UNVERIFIED']
    if documents is None:
        limitations.append('RETRIEVAL_TELEMETRY_UNAVAILABLE')
    else:
        limitations.append('EXACT_QUOTE_MATCH_IS_NOT_SEMANTIC_RETRIEVAL_EVALUATION')
    if not context['available']:
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
        if hypothesis['area'] == 'PROMPT' and not context['available']:
            continue
        hypotheses.append({'conditionId': decision['conditionId'], **hypothesis})
    return {'causeStatus': 'HYPOTHESIS' if hypotheses else 'UNDETERMINED',
            'confirmedCause': None, 'facts': facts, 'hypotheses': hypotheses, 'limitations': limitations}


def summarize(run, items, reviews=None, answer_reviews=None):
    result = {}
    # Compute once per item, then partition by side. All counts precede pagination.
    analyses = [item_analysis(run, item, reviews, answer_reviews) for item in items]
    for name in side_names(run.inputs):
        statuses = Counter()
        kinds = Counter()
        screen = Counter()
        causes = Counter()
        analysis_statuses = Counter()
        review = 0
        for details in analyses:
            analysis = details[name]
            statuses[analysis['status']] += 1
            screen[analysis['screenStatus']] += 1
            causes.update(analysis['activeHypothesisAreas'])
            analysis_statuses[analysis['analysisStatus']] += 1
            review += analysis['needsReview']
            kinds.update(issue['kind'] for issue in analysis['issues'])
        result[name] = {'statuses': {key: statuses[key] for key in STATUSES},
                        'needsReviewCount': review, 'issueTypes': dict(kinds),
                        'screenCounts': {**{field: screen[key] for key, field in SCREEN_COUNTS.items()},
                                         'total': len(items)},
                        'causeQuestionCounts': {area: causes[area] for area in CAUSE_AREAS},
                        'analysisStatusCounts': {status: analysis_statuses[status] for status in ANALYSIS_STATUSES}}
    return {'variants': result, 'issueCountsIncludeMultipleIssuesPerQuestion': True,
            'causeCountsIncludeMultipleAreasPerQuestion': True, 'causeCountUnit': 'QUESTION',
            'basis': 'LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC', 'criterionPassRatesBasis': 'AUTOMATIC'}
