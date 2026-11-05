"""Whole-answer O/X has its own audit trail and shares concurrency with conditions."""
from copy import deepcopy
from datetime import datetime

from sqlalchemy import select

from app.models.evaluation import Evaluation, EvaluationItem, EvaluationAnswerReview
from test_condition_evaluation import ConditionJudge, run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401


def test_whole_answer_ox_preserves_conditions_and_surfaces_conflicts(single_ready):
    path, before = run_condition(single_ready, FailingConditionJudge())
    factory, client, headers, *_ = single_ready
    endpoint = path + '/items/' + before['id']
    payload = {'expectedRevision': 0, 'verdict': 'pass', 'reason': '챗봇 답변 전체 정답으로 검토'}
    saved = client.post(endpoint + '/answer-reviews', headers=headers, json=payload)
    assert saved.status_code == 201, saved.text
    assert saved.json()['data']['reviewedBy']
    after = client.get(endpoint, headers=headers).json()['data']
    assert after['a'] == before['a'] and after['latestReviews'] == before['latestReviews']
    assert after['reviewRevision'] == 1
    assert after['analysis']['a']['answerReview']['verdict'] == 'pass'
    assert after['analysis']['a']['reviewConflict'] is True
    assert after['analysis']['a']['derivedScreenStatus'] == 'INCORRECT'
    assert after['screenStatus'] == 'REVIEW_REQUIRED' and after['needsReview'] is True
    assert after['resultStatus'] == 'UNMET'  # Existing condition-based contract.
    counts = client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['screenCounts']
    assert counts['reviewRequired'] == 1 and counts['correct'] == counts['incorrect'] == 0
    condition = before['input']['expectedConditions'][0]['id']
    assert client.post(endpoint + '/reviews', headers=headers, json={
        'expectedRevision': 0, 'conditionId': condition, 'verdict': 'pass', 'reason': '오래된 검토'}).status_code == 409
    assert client.post(endpoint + '/reviews', headers=headers, json={
        'expectedRevision': 1, 'conditionId': condition, 'verdict': 'pass', 'reason': '조건 확인'}).status_code == 201
    after = client.get(endpoint, headers=headers).json()['data']
    assert after['screenStatus'] == 'CORRECT' and after['analysis']['a']['reviewConflict'] is False
    assert after['reviewRevision'] == 2
    # A later whole-answer X conflicts with all-pass condition/criterion decisions.
    assert client.post(endpoint + '/answer-reviews', headers=headers, json={
        **payload, 'expectedRevision': 2, 'verdict': 'fail'}).status_code == 201
    assert client.get(endpoint, headers=headers).json()['data']['screenStatus'] == 'REVIEW_REQUIRED'
    with factory() as db:
        for row in db.scalars(select(EvaluationAnswerReview)):
            row.created_at = datetime(2026, 11, 5)
        db.commit()
    page = client.get(endpoint + '/answer-reviews', headers=headers, params={'limit': 1}).json()['data']
    assert page['items'][0]['revision'] == 3 and page['nextCursor']
    older = client.get(endpoint + '/answer-reviews', headers=headers, params={'cursor': page['nextCursor']}).json()['data']['items']
    assert len(older) == 1 and older[0]['revision'] == 1


def test_answer_review_can_resolve_judge_error_for_grading_and_wrong_notes(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    endpoint = path + '/items/' + detail['id']
    body = {'expectedRevision': 0, 'verdict': 'pass', 'reason': '정답 확인'}
    with factory() as db:
        item = db.get(EvaluationItem, detail['id'])
        value = deepcopy(item.a)
        value['criteria']['policy'].update(status='error', verdict=None, evidence_refs=[],
                                          error={'code': 'JUDGE_FAILED', 'message': '판정 실패'})
        item.a = value
        db.commit()
    assert client.post(endpoint + '/answer-reviews', headers=headers, json=body).status_code == 201
    detail = client.get(endpoint, headers=headers).json()['data']
    assert detail['screenStatus'] == 'CORRECT'
    assert detail['analysis']['a']['derivedScreenStatus'] == 'JUDGMENT_ERROR'
    wrong = client.get(path + '/items', headers=headers, params={'screenStatus': 'INCORRECT'}).json()['data']['items']
    assert wrong == []
    assert client.post(endpoint + '/answer-reviews', headers=headers,
                       json={**body, 'expectedRevision': 1, 'verdict': 'fail'}).status_code == 201
    wrong = client.get(path + '/items', headers=headers, params={'screenStatus': 'INCORRECT'}).json()['data']['items']
    assert [item['id'] for item in wrong] == [detail['id']]
    with factory() as db:
        item = db.get(EvaluationItem, detail['id'])
        item.a = {**item.a, 'callStatus': 'FAILED'}
        db.commit()
    response = client.post(endpoint + '/answer-reviews', headers=headers, json={**body, 'expectedRevision': 2})
    assert response.status_code == 409 and response.json()['error']['code'] == 'ANSWER_UNAVAILABLE'


def test_answer_review_validation_ownership_and_review_pending(single_ready):
    path, detail = run_condition(single_ready, ConditionJudge())
    factory, client, headers, *_ = single_ready
    endpoint = path + '/items/' + detail['id'] + '/answer-reviews'
    payload = {'expectedRevision': 0, 'verdict': 'not_evaluable', 'reason': '검토 보류'}
    from test_projects import register
    outsider = register(client, 'answer_review_outsider')
    assert client.post(endpoint, headers=outsider, json=payload).status_code == 404
    assert client.get(endpoint, headers=outsider).status_code == 404
    for extra in ({'side': 'b'}, {'reason': ' '}, {'expectedRevision': True}, {'verdict': 'O'}, {'conditionId': 'foreign'}):
        assert client.post(endpoint, headers=headers, json={**payload, **extra}).status_code == 400
    assert client.post(endpoint, headers=headers, json=payload).status_code == 201
    assert client.get(path + '/items/' + detail['id'], headers=headers).json()['data']['screenStatus'] == 'REVIEW_REQUIRED'
    with factory() as db:
        run = db.get(Evaluation, path.rsplit('/', 1)[-1])
        run.status = 'RUNNING'
        db.commit()
    response = client.post(endpoint, headers=headers, json={**payload, 'expectedRevision': 1})
    assert response.status_code == 409 and response.json()['error']['code'] == 'EVALUATION_NOT_TERMINAL'
