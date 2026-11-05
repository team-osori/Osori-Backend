"""Join failed answers to frozen expectations and source evidence for the product UI."""
from collections import Counter

from app.services.evaluations import side_names
from app.services.condition_evaluation import normalize_condition

STATUSES = ('MET', 'UNMET', 'REVIEW_REQUIRED', 'JUDGMENT_ERROR', 'EXECUTION_ERROR', 'NOT_RUN', 'IN_PROGRESS')


def describe_side(question, side):
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
                  'judgment': decision}
        conditions.append(record)
        if decision and decision['verdict'] == 'fail' and decision.get('issue'):
            issues.append({**record, **decision['issue'], 'reason': decision['reason']})
    results = list((side or {}).get('criteria', {}).values()) + (side or {}).get('conditionResults', [])
    errors = [result for result in results if result['status'] == 'error']
    unavailable = [result for result in results if result['status'] == 'not_evaluable']
    return {'status': status, 'needsReview': status not in ('MET', 'NOT_RUN', 'IN_PROGRESS') or bool(errors or unavailable),
            'hasJudgmentError': bool(errors), 'hasUnevaluableCriteria': bool(unavailable),
            'conditions': conditions, 'issues': issues}


def item_analysis(run, item):
    return {name: {**describe_side(item.input, getattr(item, name)),
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


def summarize(run, items):
    result = {}
    for name in side_names(run.inputs):
        statuses = Counter()
        kinds = Counter()
        review = 0
        for item in items:
            analysis = describe_side(item.input, getattr(item, name))
            statuses[analysis['status']] += 1
            review += analysis['needsReview']
            kinds.update(issue['kind'] for issue in analysis['issues'])
        result[name] = {'statuses': {key: statuses[key] for key in STATUSES},
                        'needsReviewCount': review, 'issueTypes': dict(kinds)}
    return {'variants': result, 'issueCountsIncludeMultipleIssuesPerQuestion': True}
