"""Opt-in concurrency checks against a disposable MySQL database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from test_mysql_auth_integration import mysql_client  # noqa: F401
from test_policy_testsets import setup
from test_question_conditions import assert_review_timestamp_consistency


def test_mysql_review_timestamp_matches_persisted_question_and_audit(mysql_client, monkeypatch):
    """Check review audit consistency after a real MySQL DATETIME round trip."""
    client, _, _, _ = mysql_client
    assert_review_timestamp_consistency(client, monkeypatch, login='q' + uuid4().hex[:20])


def test_mysql_concurrent_edit_records_only_winning_change(mysql_client):
    client, _, _, _ = mysql_client
    headers, _, _, _, _, path, question = setup(client, login='q' + uuid4().hex[:20])
    added = client.post(path + '/questions', json={**question, 'expectedRevision': 1}, headers=headers)
    qpath = added.headers['location']
    barrier = Barrier(2)

    def edit(text):
        barrier.wait(timeout=10)
        return client.patch(qpath, json={**question, 'question': text, 'expectedRevision': 2}, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(edit, ['취소 가능한가요?', '취소 기한은 언제인가요?']))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r for r in responses if r.status_code == 200).json()['data']['questions'][0]
    events = client.get(qpath + '/history', headers=headers).json()['data']['items']
    assert [e['action'] for e in events] == ['edited', 'created']
    assert events[0]['after'] == winner
    review = client.post(qpath + '/review', json={'expectedRevision': 3, 'status': 'approved'}, headers=headers)
    assert review.status_code == 200
    page = client.get(qpath + '/history?limit=1', headers=headers).json()['data']
    assert page['items'][0]['versionRevision'] == 4
    page = client.get(qpath + '/history', params={'limit': 1, 'cursor': page['nextCursor']}, headers=headers).json()['data']
    assert page['items'][0]['versionRevision'] == 3
    assert client.post(path + '/confirm', json={'expectedRevision': 4}, headers=headers).status_code == 200
    frozen = client.get(path + '/evaluation-snapshot', headers=headers).json()['data']
    assert frozen['schemaVersion'] == 2
    assert frozen['questions'][0]['expectedConditions'] == winner['expectedConditions']
