"""Figma grid guarantees over #37's existing screen state and answer-review contract."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models.evaluation import Evaluation, EvaluationItem, EvaluationAnswerReview
from app.models.project import Project
from app.services.auth import now
from app.services.project_lifecycle import purge_expired_projects
from test_condition_evaluation import ConditionJudge, run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401
from test_evaluations import submit


def test_grading_grid_reuses_summary_states_and_accepts_button_only_ox(single_ready):
    path, original = run_condition(single_ready, FailingConditionJudge())
    _, client, headers, *_ = single_ready
    response = client.get(path + '/grading', headers=headers)
    assert response.status_code == 200, response.text
    board = response.json()['data']
    summary = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']
    assert board['counts'] == summary['screenCounts']
    assert board['gradedQuestions'] == board['processedQuestions'] == 1
    assert board['canOpenWrongAnswers']
    tile = board['items'][0]
    assert tile['questionNumber'] == 1 and tile['screenStatus'] == 'INCORRECT' and tile['canReview']
    assert 'answer' not in tile and 'conditions' not in tile
    endpoint = path + '/items/' + tile['id']
    assert client.post(endpoint + '/answer-reviews', headers=headers,
        json={'expectedRevision': 0, 'verdict': 'pass'}).status_code == 201
    detail = client.get(endpoint, headers=headers).json()['data']
    assert detail['analysis']['a']['answerReview']['reason'] is None
    assert detail['a'] == original['a'] and detail['latestReviews'] == []
    assert detail['questionNumber'] == 1 and detail['previousItemId'] is detail['nextItemId'] is None
    updated = client.get(path + '/grading', headers=headers).json()['data']
    assert updated['items'][0]['screenStatus'] == 'REVIEW_REQUIRED'
    assert updated['items'][0]['reviewConflict']
    assert updated['counts'] == client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['screenCounts']


def test_grid_order_navigation_and_query_count_ignore_uuid_order(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    engine = factory.kw['bind']
    queries = []

    def record(*args):
        queries.append(args[2])

    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert client.get(path + '/grading', headers=headers).status_code == 200
        count = len(queries)
        item_queries = [sql for sql in queries if 'FROM evaluation_items' in sql]
        assert len(item_queries) == 1 and 'evaluation_items.a AS' not in item_queries[0]
        assert 'evaluation_items.b' not in item_queries[0]
        with factory() as db:
            run = db.get(Evaluation, path.rsplit('/', 1)[1])
            first = db.get(EvaluationItem, detail['id'])
            source = deepcopy(run.inputs)
            for index in range(10):
                question = {**deepcopy(first.input), 'id': f'next-question-{index}'}
                source['testSet']['questions'].append(question)
                db.add(EvaluationItem(id=f'000-item-{index}', evaluation_id=run.id, question_id=question['id'],
                    input=question, status='QUEUED', created_at=first.created_at))
            run.inputs, run.total_questions, run.status = source, 11, 'RUNNING'
            db.commit()
        queries.clear()
        response = client.get(path + '/grading', headers=headers)
        assert response.status_code == 200, response.text
        board = response.json()['data']
        assert len(queries) == count
        assert [tile['questionNumber'] for tile in board['items']] == list(range(1, 12))
        assert board['items'][0]['id'] == detail['id']
        assert not board['canOpenWrongAnswers'] and board['counts']['remaining'] == 10
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    first = client.get(path + '/items/' + detail['id'], headers=headers).json()['data']
    second = client.get(path + '/items/' + first['nextItemId'], headers=headers).json()['data']
    assert first['previousItemId'] is None and second['previousItemId'] == first['id']
    assert second['questionNumber'] == 2


def test_grid_ownership_cancelled_not_run_and_profile_validation(single_ready):
    from test_projects import register
    _, client, headers, service, body, original = single_ready
    path = submit(client, headers, service, body)
    board = client.get(path + '/grading', headers=headers).json()['data']
    assert board['processedQuestions'] == board['gradedQuestions'] == board['notRunQuestions'] == 0
    assert board['counts']['remaining'] == 1 and not board['items'][0]['canReview']
    assert client.post(path + '/cancel', headers=headers, json={}).status_code == 200
    cancelled = client.get(path + '/grading', headers=headers).json()['data']
    assert cancelled['notRunQuestions'] == 1 and cancelled['processedQuestions'] == 0
    assert not cancelled['canOpenWrongAnswers']
    other = register(client, 'grid_intruder')
    assert client.get(path + '/grading', headers=other).status_code == 404
    compare = submit(client, headers, service, original, key='compare-grid')
    assert client.get(compare + '/grading', headers=headers).json()['error']['code'] == 'SINGLE_EVALUATION_REQUIRED'


def test_missing_profile_criterion_is_not_grid_correct(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    with factory() as db:
        item = db.get(EvaluationItem, detail['id'])
        result = deepcopy(item.a)
        del result['criteria']['communicationQuality']
        item.a = result
        db.commit()
    response = client.get(path + '/grading', headers=headers)
    assert response.status_code == 200, response.text
    board = response.json()['data']
    assert board['counts']['correct'] == 0 and board['counts']['reviewRequired'] == 1


def test_expired_project_purge_removes_answer_review_before_item(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, service, *_ = single_ready
    endpoint = path + '/items/' + detail['id'] + '/answer-reviews'
    assert client.post(endpoint, headers=headers, json={'expectedRevision': 0, 'verdict': 'pass'}).status_code == 201
    project_id = service.split('/services/')[0].rsplit('/', 1)[1]
    with factory() as db:
        db.get(Project, project_id).deleted_at = now() - timedelta(days=31)
        db.commit()
        assert purge_expired_projects(db) == 1
        db.commit()
        assert db.get(Project, project_id) is None
        assert db.scalar(select(EvaluationAnswerReview.id)) is None


@pytest.mark.parametrize('reason', ['', ' '])
def test_optional_review_reason_still_rejects_explicit_blank(single_ready, reason):
    path, detail = run_condition(single_ready, ConditionJudge())
    _, client, headers, *_ = single_ready
    response = client.post(path + '/items/' + detail['id'] + '/answer-reviews', headers=headers,
        json={'expectedRevision': 0, 'verdict': 'pass', 'reason': reason})
    assert response.status_code == 400
