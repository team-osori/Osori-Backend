"""Issue #27: imported drafts reuse review, condition and snapshot guarantees."""
from copy import deepcopy

from app.models.policy import TestSetVersion as Version
from tests.test_policy_testsets import setup


def test_unlinked_answer_draft_requires_evidence_and_resets_approval(db_and_client):
    factory, client = db_and_client
    headers, _, _, _, _, path, grounded = setup(client)
    body = {'question': '문서에 없는 혜택도 있나요?', 'category': 'missing_document', 'modelAnswer': '확인이 필요합니다.'}
    added = client.post(path + '/questions', json={**body, 'expectedRevision': 1}, headers=headers)
    assert added.status_code == 201, added.text
    qpath = added.headers['location']
    data = added.json()['data']
    assert (data['questionCount'], data['approvedCount']) == (1, 0)
    assert data['questions'][0]['expectedConditions'] is None
    assert data['questions'][0]['expectedBehavior'] == body['modelAnswer']
    blocked = client.post(qpath + '/review', json={'expectedRevision': 2, 'status': 'approved'}, headers=headers)
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'QUESTION_EVIDENCE_REQUIRED'
    assert client.get(path, headers=headers).json()['data']['revision'] == 2
    linked = {**grounded, **body, 'expectedRevision': 2}
    edited = client.patch(qpath, json=linked, headers=headers)
    assert edited.status_code == 200, edited.text
    conditions = edited.json()['data']['questions'][0]['expectedConditions']
    approved = client.post(qpath + '/review', json={'expectedRevision': 3, 'status': 'approved'}, headers=headers)
    assert approved.json()['data']['approvedCount'] == 1
    changed = client.patch(qpath, json={**linked, 'expectedRevision': 4, 'modelAnswer': '새 예시 답안',
        'expectedConditions': conditions}, headers=headers)
    assert changed.json()['data']['approvedCount'] == 0
    q = changed.json()['data']['questions'][0]
    assert q['expectedConditions'] == conditions and q['expectedBehavior'] == grounded['expectedBehavior']
    client.post(qpath + '/review', json={'expectedRevision': 5, 'status': 'approved'}, headers=headers)
    assert client.post(path + '/confirm', json={'expectedRevision': 6}, headers=headers).status_code == 200
    snapshot = client.get(path + '/evaluation-snapshot', headers=headers).json()['data']
    assert snapshot['schemaVersion'] == 2
    assert snapshot['questions'][0]['modelAnswer'] == '새 예시 답안'
    assert snapshot['questions'][0]['expectedConditions'] == conditions
    # Historical snapshots omit the new optional field and remain byte-for-byte JSON equivalent.
    with factory() as db:
        version = db.get(Version, data['id'])
        legacy = deepcopy(version.snapshot)
        legacy['questions'][0].pop('modelAnswer')
        version.snapshot = legacy
        db.commit()
    assert client.get(path + '/evaluation-snapshot', headers=headers).json()['data'] == legacy


def test_partial_evidence_and_unlinked_conditions_are_rejected(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, question = setup(client)
    for fields in ({'policyVersionId': question['policyVersionId']}, {'ruleKeys': ['refund']},
                   {'evidenceQuote': question['evidenceQuote']}):
        result = client.post(path + '/questions', json={'question': '질문', 'modelAnswer': '답안',
            'expectedRevision': 1, **fields}, headers=headers)
        assert result.status_code == 400
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
