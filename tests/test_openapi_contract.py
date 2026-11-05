from app.main import app
from test_single_evaluation import single_ready  # noqa: F401
from test_condition_evaluation import ConditionJudge, run_condition


def test_openapi_exposes_request_constraints_and_typed_results():
    schema = app.openapi()
    models = schema['components']['schemas']
    assert models['ConnectionPut']['properties']['executionMode']['enum'] == ['PROMPT_BODY', 'CONNECTION_ONLY']
    assert 'PROMPT_BODY' in models['ConnectionTestCreate']['properties']['promptVersionId']['description']
    headers = []
    for path, operations in schema['paths'].items():
        if 'delete' in operations:
            headers.extend(p for p in operations['delete'].get('parameters', []) if p['in'] == 'header')
    assert len(headers) >= 3
    for header in headers:
        assert header['name'] == 'If-Match'
        assert header['schema']['pattern'] == '^"[1-9][0-9]*"$'
        assert '"3"' in header['description']
    root = '/api/projects/{projectId}/services/{serviceId}/evaluations/{evaluationId}'
    params = {p['name']: p for p in schema['paths'][root + '/items']['get']['parameters']}
    assert 'criterion' in params['change']['description']
    assert '빈 목록' in params['criterion']['description']
    comparison = schema['paths'][root + '/comparison']['get']
    baseline = next(p for p in comparison['parameters'] if p['name'] == 'baselineEvaluationId')
    assert 'BASELINE_REQUIRED' in baseline['description']
    assert comparison['responses']['200']['content']['application/json']['schema']['$ref'].endswith('Data_EvaluationComparison_')
    assert 'SINGLE' in schema['paths'][root + '/reruns']['post']['description']
    for model in ('EvaluationSummary', 'EvaluationSide'):
        choices = models[model]['properties']['criteria']['anyOf']
        assert len(choices) == 2 and all('$ref' in choice for choice in choices)
    assert set(models['ConditionCriteriaOutput']['properties']) == {'policy', 'questionResolution', 'communicationQuality'}
    assert models['ScoredCriterionOutput']['properties']['score']


def test_unavailable_criterion_returns_empty_page_and_constraints_remain_explicit(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    _, client, headers, *_ = single_ready
    response = client.get(path + '/items', params={'criterion': 'retrieval'}, headers=headers)
    assert response.status_code == 200
    assert response.json()['data'] == {'items': [], 'nextCursor': None}
    response = client.get(path + '/items', params={'change': 'IMPROVED'}, headers=headers)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    response = client.get(path + '/comparison', headers=headers)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'BASELINE_REQUIRED'
    # Typed serialization preserves scores and exposes only the actual profile.
    assert set(detail['a']['criteria']) == {'policy', 'questionResolution', 'communicationQuality'}
    assert detail['a']['criteria']['questionResolution']['score'] == 4
