"""A generation API -> frozen conditions -> B judge -> review/re-evaluation, without network."""
import asyncio
import json
from copy import deepcopy

from app import evaluation_worker as worker
from app.main import app
from app.models.policy import TestSetVersion as VersionRow
from app.services.question_generation import get_question_generator
from app.services.evaluation_judge import get_evaluation_judge
from app.services.openai_evaluation_judge import OpenAIEvaluationJudge
from test_condition_evaluation import ConditionJudge, answer
from test_evaluation_analysis import FailingConditionJudge
from test_openai_evaluation_judge import completed
from test_single_evaluation import single_ready  # noqa: F401
from test_evaluations import submit


def test_generated_a_conditions_flow_through_b_analysis_and_frozen_rerun(single_ready, monkeypatch):
    factory, client, headers, service, old_body, original = single_ready
    project = service.split('/services/')[0]
    with factory() as db:
        old = deepcopy(db.get(VersionRow, old_body['testSetVersionId']).snapshot)
    template = old['questions'][0]
    source = {key: template[key] for key in ('policyVersionId', 'ruleKeys', 'evidenceQuote')}
    conditions = [{'description': template['expectedBehavior'], 'kind': 'required', 'category': 'policy', **source},
                  {'description': template['forbiddenBehaviors'][0], 'kind': 'forbidden', 'category': 'policy', **source}]

    class Generator:
        def generate(self, *, policies, count, categories):
            assert policies[0]['id'] == source['policyVersionId']
            return {'questions': [{'question': template['question'] + ' ' + category,
                'expectedBehavior': template['expectedBehavior'], 'forbiddenBehaviors': template['forbiddenBehaviors'],
                'category': category, 'expectedConditions': conditions, **source} for category in categories]}

    app.dependency_overrides[get_question_generator] = lambda: Generator()
    created = client.post(project + '/testsets', headers=headers,
                          json={'name': 'A/B 통합 검증', 'policyVersionIds': [source['policyVersionId']]})
    version = created.json()['data']['version']
    version_path = created.headers['location']
    generated = client.post(version_path + '/questions/generate', headers={**headers, 'Idempotency-Key': 'a-generation'},
                            json={'expectedRevision': 1, 'count': 3, 'categories': ['normal', 'exception', 'boundary']})
    assert generated.status_code == 200, generated.text
    draft = generated.json()['data']
    for question in draft['questions']:
        reviewed = client.post(version_path + '/questions/' + question['id'] + '/review', headers=headers,
                               json={'expectedRevision': draft['revision'], 'status': 'approved', 'note': '정책 근거 확인'})
        assert reviewed.status_code == 200
        draft = reviewed.json()['data']
    confirmed = client.post(version_path + '/confirm', headers=headers, json={'expectedRevision': draft['revision']})
    assert confirmed.status_code == 200
    a_snapshot = client.get(version_path + '/evaluation-snapshot', headers=headers).json()['data']
    assert a_snapshot['schemaVersion'] == 2
    assert {question['category'] for question in a_snapshot['questions']} == {'normal', 'exception', 'boundary'}

    judge = OpenAIEvaluationJudge('test-only-key', 'test-model')
    phase = ['before']

    async def request(body):
        data = json.loads(body['input'][0]['content'])
        test_judge = FailingConditionJudge() if phase[0] == 'before' else ConditionJudge()
        return completed(await test_judge.judge(**data))

    monkeypatch.setattr(judge, '_request', request)
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, {'connectionRevision': 2, 'testSetVersionId': version['id']}, key='b-source')
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    assert len(items) == 3 and all(item['resultStatus'] == 'UNMET' for item in items)
    b_snapshot = client.get(path + '/inputs', headers=headers).json()['data']['testSet']
    assert [c['id'] for q in b_snapshot['questions'] for c in q['expectedConditions']] == [
        c['id'] for q in a_snapshot['questions'] for c in q['expectedConditions']]
    detail = client.get(path + '/items/' + items[0]['id'], headers=headers).json()['data']
    assert detail['analysis']['a']['issues'][0]['policyEvidence']['quote'] == source['evidenceQuote']
    assert detail['analysis']['a']['causeStatus'] == 'UNDETERMINED'

    # A may start editing its next version while B must keep the original evaluation inputs.
    clone = client.post(project + '/testsets/' + a_snapshot['testSetId'] + '/versions', headers=headers,
        json={'expectedRevision': 1, 'sourceVersionId': version['id']})
    assert clone.status_code == 201
    clone_data = clone.json()['data']
    question = clone_data['questions'][0]
    edited = client.patch(clone.headers['location'] + '/questions/' + question['id'], headers=headers,
        json={'expectedRevision': clone_data['revision'], 'question': '새 버전의 다른 질문',
              **{key: question[key] for key in ('expectedBehavior', 'forbiddenBehaviors', 'policyVersionId',
                                               'ruleKeys', 'evidenceQuote', 'category', 'expectedConditions')}})
    assert edited.status_code == 200
    phase[0] = 'after'
    rerun = client.post(path + '/reruns', headers={**headers, 'Idempotency-Key': 'b-rerun'}, json={'connectionRevision': 2})
    assert rerun.status_code == 202, rerun.text
    target = rerun.headers['location']
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    assert client.get(target + '/inputs', headers=headers).json()['data']['testSet'] == b_snapshot
    result = client.get(target + '/comparison', headers=headers).json()['data']
    assert result['counts']['RESOLVED'] == 3 and result['counts']['NEW'] == 0
    assert result['counts']['UNCHANGED_PASS'] == 3 and result['outcome'] == 'IMPROVED'
