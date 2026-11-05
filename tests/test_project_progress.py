from datetime import timedelta

import asyncio
import pytest
from sqlalchemy import event, select

from app.models.evaluation import Evaluation
from app.models.policy import TestSetVersion as SetVersion
from app.models.project import Project
from app.services.auth import now
from test_condition_evaluation import ConditionJudge, run_condition
from test_evaluations import submit
from test_policy_testsets import setup
from test_projects import register
from test_single_evaluation import single_ready  # noqa: F401


def progress(client, headers, project):
    response = client.get(project, headers=headers)
    assert response.status_code == 200, response.text
    single = response.json()['data']['progress']
    listed = client.get('/api/projects', headers=headers).json()['data']['items']
    assert next(row for row in listed if row['id'] == project.rsplit('/', 1)[1])['progress'] == single
    return single


def test_draft_confirmation_and_new_draft_preserve_unlock(db_and_client):
    _, client = db_and_client
    headers, project, _, _, data, path, question = setup(client)
    current = progress(client, headers, project)
    assert current['stage'] == current['unlockedStage'] == 1
    assert current['status'] == 'SETUP'
    assert current['testSet']['questionCount'] == 0
    assert current['evaluation'] is current['lastEvaluatedAt'] is current['promptVersion'] is None
    response = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    assert response.status_code == 201
    assert client.post(response.headers['location'] + '/review',
        json={'expectedRevision': 2, 'status': 'approved'}, headers=headers).status_code == 200
    assert client.post(path + '/confirm', json={'expectedRevision': 3}, headers=headers).status_code == 200
    current = progress(client, headers, project)
    assert current['stage'] == current['unlockedStage'] == 2
    assert current['testSet']['questionCount'] == 1
    response = client.post(project + '/testsets/' + data['testSet']['id'] + '/versions',
        json={'expectedRevision': 1, 'sourceVersionId': data['version']['id']}, headers=headers)
    assert response.status_code == 201
    current = progress(client, headers, project)
    assert current['unlockedStage'] == 2
    assert current['testSet'] == {'versionId': response.json()['data']['id'],
        'label': 'v2', 'status': 'draft', 'questionCount': 1}


def exercise_finished_progress(factory, client, headers, service, path, detail):
    project = service.split('/services/')[0]
    current = progress(client, headers, project)
    assert current['stage'] == current['unlockedStage'] == 3
    assert current['status'] == 'DONE' and current['lastEvaluatedAt']
    assert current['promptVersion'] is None
    assert current['evaluation'] == {'id': path.rsplit('/', 1)[1], 'status': 'COMPLETED',
        'completedQuestions': 1, 'totalQuestions': 1, 'reviewedQuestions': 0}
    endpoint = path + '/items/' + detail['id'] + '/reviews'
    conditions = detail['input']['expectedConditions']
    for revision, condition in enumerate(conditions):
        response = client.post(endpoint, headers=headers, json={'expectedRevision': revision,
            'conditionId': condition['id'], 'verdict': 'pass', 'reason': '검토 완료'})
        assert response.status_code == 201, response.text
        expected = int(revision == len(conditions) - 1)
        assert progress(client, headers, project)['evaluation']['reviewedQuestions'] == expected
    response = client.post(endpoint, headers=headers, json={'expectedRevision': len(conditions),
        'conditionId': conditions[0]['id'], 'verdict': 'not_evaluable', 'reason': '재검토'})
    assert response.status_code == 201
    assert progress(client, headers, project)['evaluation']['reviewedQuestions'] == 0


def test_completed_progress_and_latest_human_reviews(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, service, *_ = single_ready
    exercise_finished_progress(factory, client, headers, service, path, detail)
    with factory() as db:
        version = db.scalar(select(SetVersion))
        db.add(SetVersion(id='later-draft', testset_id=version.testset_id, version=2, revision=1,
            status='draft', policy_version_ids=version.policy_version_ids, created_at=now() + timedelta(seconds=1)))
        db.commit()
    assert progress(client, headers, service.split('/services/')[0])['unlockedStage'] == 3


@pytest.mark.parametrize('status', ['QUEUED', 'RUNNING', 'FAILED'])
def test_active_rerun_preserves_results_unlock_and_prompt_version(single_ready, status):
    path, _ = run_condition(single_ready, ConditionJudge())
    factory, client, headers, service, body, original = single_ready
    new_path = submit(client, headers, service, {**body, 'promptVersionId': original['aPromptVersionId']}, key='next')
    with factory() as db:
        row = db.get(Evaluation, new_path.rsplit('/', 1)[1])
        row.created_at = now() + timedelta(seconds=1)
        row.status = status
        db.commit()
    current = progress(client, headers, service.split('/services/')[0])
    assert current['stage'] == 2 and current['unlockedStage'] == 3
    assert current['status'] == 'RUNNING' and current['lastEvaluatedAt']
    assert current['evaluation']['id'] == new_path.rsplit('/', 1)[1]
    assert current['promptVersion'] == 1
    other = register(client, 'progress_intruder')
    assert client.get(service.split('/services/')[0], headers=other).status_code == 404
    assert client.get('/api/projects', headers=other).json()['data']['items'] == []


def test_compare_requires_reviews_on_both_sides_and_partial_failure_unlocks(single_ready):
    from app import evaluation_worker as worker
    from app.main import app
    from app.services.evaluation_judge import get_evaluation_judge
    from test_condition_evaluation import answer
    factory, client, headers, service, _, original = single_ready
    judge = ConditionJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, {**original, 'mode': 'COMPARE'})
    asyncio.run(worker.process_one(factory, sender=answer, judge=judge))
    item = client.get(path + '/items', headers=headers).json()['data']['items'][0]
    detail = client.get(path + '/items/' + item['id'], headers=headers).json()['data']
    revision = 0
    project = service.split('/services/')[0]
    for side in ('a', 'b'):
        for condition in detail['input']['expectedConditions']:
            response = client.post(path + '/items/' + item['id'] + '/reviews', headers=headers,
                json={'expectedRevision': revision, 'side': side, 'conditionId': condition['id'],
                    'verdict': 'pass', 'reason': '검토 완료'})
            assert response.status_code == 201, response.text
            revision += 1
        assert progress(client, headers, project)['evaluation']['reviewedQuestions'] == int(side == 'b')
    with factory() as db:
        row = db.get(Evaluation, path.rsplit('/', 1)[1])
        row.status = 'PARTIAL_FAILED'
        db.commit()
    current = progress(client, headers, project)
    assert current['stage'] == current['unlockedStage'] == 3 and current['status'] == 'DONE'


def test_project_list_query_count_does_not_grow_with_project_count(single_ready):
    run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    engine = factory.kw['bind']
    queries = []

    def record(*args):
        queries.append(args[2])

    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert client.get('/api/projects', headers=headers).status_code == 200
        count = len(queries)
        with factory() as db:
            source = db.scalar(select(Project))
            for index in range(10):
                db.add(Project(id=f'project-{index}', owner_id=source.owner_id, name='Other', revision=1,
                    created_at=now(), updated_at=now()))
            db.commit()
        queries.clear()
        response = client.get('/api/projects', headers=headers)
        assert response.status_code == 200
        assert len(response.json()['data']['items']) == 11
        assert len(queries) == count
    finally:
        event.remove(engine, 'before_cursor_execute', record)
