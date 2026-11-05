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


def upload(client, path, headers, content, revision=1, filename='questions.csv'):
    return client.post(path + '/questions/import', headers=headers, data={'expectedRevision': str(revision)},
                       files={'file': (filename, content, 'text/csv')})


def test_csv_bom_multiline_korean_types_and_atomic_retry(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, _ = setup(client)
    content = '\ufeff질문,유형,모범 답안\r\n"질문, 하나\n두 줄",정상,"답안, 하나"\r\n질문2,모호,답안2\r\n질문3,문서 없음,답안3\r\n질문4,정책 함정,답안4\r\n'
    result = upload(client, path, headers, content.encode())
    assert result.status_code == 201, result.text
    version = result.json()['data']
    assert version['revision'] == 2 and version['questionCount'] == 4 and version['approvedCount'] == 0
    by_type = {q['category']: q for q in version['questions']}
    assert by_type['normal']['question'] == '질문, 하나\n두 줄'
    assert by_type['normal']['modelAnswer'] == '답안, 하나'
    assert all(q['expectedConditions'] is None and q['reviewStatus'] == 'pending' for q in by_type.values())
    assert upload(client, path, headers, content.encode()).status_code == 409
    history = client.get(path + '/history', headers=headers).json()['data']['items']
    assert len(history) == 4 and all(h['note'] == 'CSV 가져오기' for h in history)
    # Existing deletion flow applies to imports as well.
    qpath = path + '/questions/' + by_type['normal']['id']
    assert client.delete(qpath, headers={**headers, 'If-Match': '"2"'}).status_code == 204
    assert client.get(path, headers=headers).json()['data']['questionCount'] == 3


def test_csv_reports_all_invalid_rows_without_saving_valid_rows(db_and_client):
    _, client = db_and_client
    headers, _, _, _, _, path, _ = setup(client)
    result = upload(client, path, headers, 'question,category,modelAnswer\n정상,normal,답안\n,wrong,\n열,개수\n')
    assert result.status_code == 400, result.text
    error = result.json()['error']
    assert error['code'] == 'CSV_VALIDATION_ERROR' and error['requestId']
    assert {e['field'] for e in error['fieldErrors']} == {'rows.3.question', 'rows.3.category', 'rows.3.modelAnswer', 'rows.4.row'}
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
    assert client.get(path + '/history', headers=headers).json()['data']['items'] == []
    assert upload(client, path, headers, 'question,category,modelAnswer\n정상,normal,답안\n').status_code == 201


def test_csv_limits_format_capacity_and_scope(db_and_client, monkeypatch):
    from app.services import question_import
    from tests.test_policy_testsets import account
    _, client = db_and_client
    headers, _, _, _, _, path, _ = setup(client)
    for content, code in [(b'\xff', 'CSV_ENCODING_INVALID'), ('', 'CSV_VALIDATION_ERROR'),
        ('question,category,modelAnswer\n', 'CSV_EMPTY'),
        ('question,category,modelAnswer\n"unterminated', 'CSV_VALIDATION_ERROR'),
        ('question,question,modelAnswer\na,b,c', 'CSV_VALIDATION_ERROR'),
        ('question,category,modelAnswer\na,normal,\x00', 'CSV_FORMAT_INVALID')]:
        result = upload(client, path, headers, content)
        assert result.status_code == 400 and result.json()['error']['code'] == code, result.text
    assert upload(client, path, headers, 'x', filename='a.xlsx').status_code == 415
    monkeypatch.setattr(question_import, 'MAX_CSV_BYTES', 10)
    assert upload(client, path, headers, 'x' * 11).status_code == 413
    monkeypatch.setattr(question_import, 'MAX_CSV_BYTES', 2 * 1024 * 1024)
    monkeypatch.setattr(question_import, 'MAX_CSV_ROWS', 1)
    content = 'question,category,modelAnswer\na,normal,b\nc,normal,d'
    assert upload(client, path, headers, content).json()['error']['code'] == 'CSV_ROW_LIMIT_EXCEEDED'
    monkeypatch.setattr(question_import, 'MAX_CSV_ROWS', 200)
    monkeypatch.setattr('app.api.testsets.MAX_QUESTIONS', 1)
    assert upload(client, path, headers, content).json()['error']['code'] == 'QUESTION_LIMIT_EXCEEDED'
    other = account(client, 'other-import')
    assert upload(client, path, other, content).status_code == 404
    assert client.get(path, headers=headers).json()['data']['revision'] == 1
