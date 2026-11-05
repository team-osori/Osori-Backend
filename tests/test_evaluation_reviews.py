from datetime import datetime

from sqlalchemy import select

from app.models.evaluation import EvaluationItem, EvaluationReview
from test_condition_evaluation import run_condition
from test_evaluation_analysis import FailingConditionJudge
from test_single_evaluation import single_ready  # noqa: F401


def test_human_review_preserves_ai_and_changes_effective_condition(single_ready):
    path, detail = run_condition(single_ready, FailingConditionJudge())
    factory, client, headers, *_ = single_ready
    endpoint = path + '/items/' + detail['id']
    condition = detail['input']['expectedConditions'][0]
    payload = {'expectedRevision': 0, 'conditionId': condition['id'], 'verdict': 'pass', 'reason': '정책 조건을 실제로 안내했습니다.'}
    result = client.post(endpoint + '/reviews', headers=headers, json=payload)
    assert result.status_code == 201, result.text
    review = result.json()['data']
    assert review['revision'] == 1 and review['reviewedBy'] and review['createdAt']
    updated = client.get(endpoint, headers=headers).json()['data']
    assert updated['a'] == detail['a'] and updated['input'] == detail['input']
    assert updated['analysis']['a']['conditions'][0]['automaticJudgment']['verdict'] == 'fail'
    assert updated['analysis']['a']['conditions'][0]['judgment']['verdict'] == 'pass'
    assert updated['resultStatus'] == 'MET' and updated['reviewRevision'] == 1
    assert client.get(path + '/summary', headers=headers).json()['data']['analysis']['variants']['a']['statuses']['MET'] == 1
    assert client.post(endpoint + '/reviews', headers=headers, json=payload).status_code == 409
    second = client.post(endpoint + '/reviews', headers=headers, json={**payload, 'expectedRevision': 1, 'verdict': 'not_evaluable', 'reason': '재검토 필요'})
    assert second.status_code == 201
    # MySQL truncates DATETIME to seconds: UUID order must not determine review order.
    with factory() as db:
        for row in db.scalars(select(EvaluationReview)):
            row.created_at = datetime(2026, 11, 5)
            row.id = ('ffffffff-ffff-4fff-8fff-ffffffffffff' if row.revision == 1
                      else '00000000-0000-4000-8000-000000000000')
        db.commit()
    history = client.get(endpoint + '/reviews', headers=headers, params={'limit': 1}).json()['data']
    assert history['items'][0]['revision'] == 2 and history['nextCursor']
    older = client.get(endpoint + '/reviews', headers=headers, params={'cursor': history['nextCursor']}).json()['data']['items']
    assert len(older) == 1 and older[0]['revision'] == 1
    assert client.get(endpoint, headers=headers).json()['data']['resultStatus'] == 'REVIEW_REQUIRED'


def test_review_ownership_condition_and_target_validation(single_ready):
    path, detail = run_condition(single_ready, FailingConditionJudge())
    factory, client, headers, *_ = single_ready
    endpoint = path + '/items/' + detail['id'] + '/reviews'
    body = {'expectedRevision': 0, 'conditionId': detail['input']['expectedConditions'][0]['id'], 'verdict': 'fail', 'reason': '조건 위반'}
    from test_projects import register
    other = register(client, 'review_intruder')
    assert client.post(endpoint, headers=other, json=body).status_code == 404
    assert client.get(endpoint, headers=other).status_code == 404
    assert client.post(endpoint, headers=headers, json={**body, 'conditionId': 'foreign'}).status_code == 400
    assert client.post(endpoint, headers=headers, json={**body, 'side': 'b'}).status_code == 400
    assert client.post(endpoint, headers=headers, json={**body, 'reason': ' '}).status_code == 400
    assert client.post(endpoint, headers=headers, json={**body, 'expectedRevision': True}).status_code == 400
    with factory() as db:
        item = db.get(EvaluationItem, detail['id'])
        assert item.review_revision == 0
