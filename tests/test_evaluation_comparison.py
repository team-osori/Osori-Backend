import asyncio
from copy import deepcopy

from app import evaluation_worker as worker
from app.main import app
from app.models.evaluation import Evaluation
from app.services.evaluation_judge import get_evaluation_judge
from test_condition_evaluation import ConditionJudge, run_condition, answer
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401


def rerun(single_ready, path, judge, key='rerun-1'):
    factory, client, headers, service, body, original = single_ready
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    response = client.post(path + '/reruns', headers={**headers, 'Idempotency-Key': key},
                           json={'connectionRevision': body['connectionRevision']})
    assert response.status_code == 202, response.text
    target = response.headers['location']
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    return target


def test_rerun_preserves_exact_sources_and_reports_resolved_conditions(single_ready):
    source, detail = run_condition(single_ready, FailingConditionJudge())
    factory, client, headers, service, body, original = single_ready
    judge = ConditionJudge()
    target = rerun(single_ready, source, judge)
    a = client.get(source + '/inputs', headers=headers).json()['data']
    b = client.get(target + '/inputs', headers=headers).json()['data']
    assert a['testSet'] == b['testSet'] and a['criteria'] == b['criteria']
    assert b['sourceEvaluationId'] == source.rsplit('/', 1)[1]
    response = client.get(target + '/comparison', headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['counts']['RESOLVED'] == 1 and result['counts']['NEW'] == 0
    assert result['outcome'] == 'IMPROVED'
    assert result['externalEnvironmentControlled'] is False
    assert result['items'][0]['conditions'][0]['conditionId'] == detail['input']['expectedConditions'][0]['id']
    assert client.post(source + '/reruns', headers={**headers, 'Idempotency-Key': 'rerun-1'}, json={'connectionRevision': 2}).headers['location'] == target


def test_new_failure_is_not_hidden_by_other_results(single_ready):
    source, detail = run_condition(single_ready, ConditionJudge())
    target = rerun(single_ready, source, FailingConditionJudge())
    factory, client, headers, *_ = single_ready
    result = client.get(target + '/comparison', headers=headers).json()['data']
    assert result['counts']['NEW'] == 1 and result['hasNewIssues'] is True
    assert result['outcome'] == 'REGRESSED'


def test_rerun_preserves_older_b_snapshot_without_adding_new_condition_defaults(single_ready):
    source, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    with factory() as db:
        row = db.get(Evaluation, source.rsplit('/', 1)[1])
        inputs = deepcopy(row.inputs)
        for condition in inputs['testSet']['questions'][0]['expectedConditions']:
            for key in ('kind', 'category', 'ruleKeys'):
                condition.pop(key, None)
        row.inputs = inputs
        db.commit()
    target = rerun(single_ready, source, ConditionJudge())
    before = client.get(source + '/inputs', headers=headers).json()['data']
    after = client.get(target + '/inputs', headers=headers).json()['data']
    assert before['testSet'] == after['testSet']
    assert client.get(target + '/comparison', headers=headers).status_code == 200


def test_comparison_rejects_changed_snapshot_and_foreign_owner(single_ready):
    source, detail = run_condition(single_ready, ConditionJudge())
    target = rerun(single_ready, source, ConditionJudge())
    factory, client, headers, *_ = single_ready
    from test_projects import register
    other = register(client, 'comparison_intruder')
    assert client.get(target + '/comparison', headers=other).status_code == 404
    with factory() as db:
        row = db.get(Evaluation, target.rsplit('/', 1)[1])
        inputs = deepcopy(row.inputs)
        inputs['testSet']['questions'][0]['expectedConditions'][0]['requirement'] = '바뀐 조건'
        row.inputs = inputs
        db.commit()
    response = client.get(target + '/comparison', headers=headers)
    assert response.status_code == 409 and response.json()['error']['code'] == 'COMPARISON_INPUT_MISMATCH'


def test_unjudged_conditions_are_not_counted_as_improvement(single_ready):
    factory, client, headers, service, body, original = single_ready
    from test_evaluations import submit
    source = submit(client, headers, service, body)
    asyncio.run(worker.process_one(factory, sender=answer))
    response = client.post(source + '/reruns', headers={**headers, 'Idempotency-Key': 'unjudged'}, json={'connectionRevision': 2})
    assert response.status_code == 202
    target = response.headers['location']
    asyncio.run(worker.process_one(factory, sender=answer))
    result = client.get(target + '/comparison', headers=headers).json()['data']
    assert result['counts']['NOT_COMPARABLE'] == 2
    assert result['outcome'] == 'INCOMPLETE'


def test_net_reduction_still_reports_new_issues_as_mixed():
    from types import SimpleNamespace
    from app.services.evaluation_comparison import compare
    conditions = [{'id': key, 'criterion': 'policy', 'requirement': '필수 조건',
                   'policyVersionId': 'p', 'sourceQuote': '정책'} for key in ('c1', 'c2', 'c3')]
    question = {'id': 'q', 'expectedConditions': conditions}
    inputs = {'mode': 'SINGLE', 'testSet': {'questions': [question]}, 'criteria': {'version': 2},
              'judge': {}, 'connection': {'config': {}}}
    baseline = SimpleNamespace(id='old', status='COMPLETED', inputs=inputs)
    target = SimpleNamespace(id='new', status='COMPLETED', inputs=deepcopy(inputs))

    def item(id_, verdicts):
        decisions = [{'conditionId': condition['id'], 'status': 'judged', 'verdict': verdict,
                      'reason': '조건 판정'} for condition, verdict in zip(conditions, verdicts)]
        return SimpleNamespace(id=id_, question_id='q', input=question, review_revision=0,
            a={'callStatus': 'SUCCEEDED', 'conditionResults': decisions,
               'criteria': {'policy': {'status': 'judged', 'verdict': 'fail'}}})
    result = compare(baseline, target, [item('before', ['fail', 'fail', 'pass'])],
                     [item('after', ['pass', 'pass', 'fail'])], {})
    assert result['counts']['RESOLVED'] == 2 and result['counts']['NEW'] == 1
    assert result['outcome'] == 'MIXED' and result['hasNewIssues'] is True
