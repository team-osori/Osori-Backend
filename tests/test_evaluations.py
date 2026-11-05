import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from app import evaluation_worker as worker
from app.main import app
from app.models.connection import Connection
from app.models.evaluation import Evaluation, EvaluationItem
from app.schemas.evaluation import Evidence
from app.services.auth import now
from app.services.chatbot_gateway import ChatbotReply, GatewayError
from app.services.evaluation_judge import CRITERIA, get_evaluation_judge
from app.services.evaluations import aggregate
from test_connections_prompts import CONFIG, prepare_connection
from test_policy_testsets import setup as setup_policy


def prepare(factory, client, monkeypatch, question_count=1):
    monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    headers, project, policy, source, testset, path, question = setup_policy(client, login='eval' + uuid4().hex[:20])
    revision = 1
    for index in range(question_count):
        added = client.post(path + '/questions', headers=headers, json={**question,
            'question': question['question'] + f' ({index + 1})', 'expectedRevision': revision})
        qpath = added.headers['location']
        assert client.post(qpath + '/review', headers=headers,
            json={'expectedRevision': revision + 1, 'status': 'approved'}).status_code == 200
        revision += 2
    assert client.post(path + '/confirm', headers=headers, json={'expectedRevision': revision}).status_code == 200
    service = client.post(project + '/services', headers=headers, json={'name': '항공 챗봇'}).headers['location']
    a = client.post(service + '/prompts', headers=headers, json={'name': '안내 지시문', 'content': 'A prompt'}).json()['data']
    b = client.post(service + '/prompts/' + a['id'] + '/versions', headers=headers,
        json={'baseVersionId': a['currentVersionId'], 'content': 'B prompt'}).json()['data']
    prepare_connection(client, headers, service)
    with factory() as db:
        row = db.scalar(select(Connection).where(Connection.service_id == service.rsplit('/', 1)[1]))
        row.verified_revision = row.revision
        db.commit()
    body = {'connectionRevision': 2, 'aPromptVersionId': a['currentVersionId'], 'bPromptVersionId': b['id'],
            'testSetVersionId': testset['version']['id']}
    return headers, service, body, path


@pytest.fixture
def ready(db_and_client, monkeypatch):
    factory, client = db_and_client
    return factory, client, *prepare(factory, client, monkeypatch)


def submit(client, headers, service, body, key='evaluation-1'):
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': key}, json=body)
    assert response.status_code == 202, response.text
    return response.headers['location']


async def plain_answer(*args):
    return '예약 취소는 출발 24시간 전까지 가능합니다.'


class TestJudge:
    __test__ = False

    def descriptor(self):
        return {'adapter': 'test-only-v1', 'provider': 'local-test', 'model': 'fake'}

    async def judge(self, *, answer, **kwargs):
        return {key: {'status': 'PASS' if answer.startswith('B:') else 'FAIL',
            'reason': '테스트 판정', 'evidence': ['출발 24시간 전까지']} for key in CRITERIA['definitions']}


def test_submission_idempotency_validation_and_ownership(ready):
    factory, client, headers, service, body, _ = ready
    path = submit(client, headers, service, body)
    assert submit(client, headers, service, body) == path
    bad = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'evaluation-1'},
                      json={**body, 'connectionRevision': 1})
    assert bad.status_code == 409 and bad.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    for change, status, code in [({'connectionRevision': 1}, 409, 'CONNECTION_CHANGED'),
                         ({'bPromptVersionId': body['aPromptVersionId']}, 400, 'SAME_PROMPT_VERSION')]:
        response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={**body, **change})
        assert response.status_code == status and response.json()['error']['code'] == code
    assert client.post(service + '/evaluations', headers=headers, json=body).status_code == 400
    assert client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'x'}, json={**body, 'connectionRevision': True}).status_code == 400
    from test_projects import register
    other = register(client, 'intruder')
    for suffix in ('', '/inputs', '/summary', '/items'):
        assert client.get(path + suffix, headers=other).status_code == 404
        assert client.get(path + suffix).status_code == 401
    with factory() as db:
        assert len(db.scalars(select(Evaluation)).all()) == 1
        connection = db.scalar(select(Connection))
        connection.verified_revision = None
        db.commit()
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'unverified'}, json=body)
    assert response.json()['error']['code'] == 'CONNECTION_NOT_VERIFIED'
    # A previously accepted request remains replayable after the connection changes.
    assert submit(client, headers, service, body) == path


def test_default_judge_preserves_frozen_sources_without_inventing_grades(ready):
    factory, client, headers, service, body, source_path = ready
    path = submit(client, headers, service, body)
    frozen = client.get(path + '/inputs', headers=headers).json()['data']
    with factory() as db:
        row = db.scalar(select(Evaluation))
        assert row.secret_ciphertext and 'test-only-secret-token' not in row.secret_ciphertext
    assert client.put(service + '/connection', headers=headers, json={**CONFIG, 'expectedRevision': 2, 'url': 'https://new.example.com/chat'}).status_code == 200
    calls = []

    async def sender(config, payload, outgoing):
        calls.append(deepcopy(payload))
        assert config['url'] == CONFIG['url']
        assert outgoing['Authorization'] == 'Bearer test-only-secret-token'
        return '답변 test-only-secret-token'

    assert asyncio.run(worker.process_one(factory, sender=sender))
    assert not asyncio.run(worker.process_one(factory, sender=sender))
    data = client.get(path, headers=headers).json()['data']
    assert data['status'] == 'COMPLETED' and data['completedQuestions'] == 1 and data['failedQuestions'] == 0
    assert data['judge']['model'] is None
    assert data['comparisonAssurance']['retrievalInputsControlled'] is False
    assert [call['messages'][0]['content'] for call in calls] == ['A prompt', 'B prompt']
    assert calls[0]['messages'][1]['content'] == calls[1]['messages'][1]['content']
    assert client.get(path + '/inputs', headers=headers).json()['data'] == frozen
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['provisional'] is False
    assert summary['criteria']['policy']['a']['passRate'] is None
    assert summary['criteria']['policy']['a']['notEvaluable'] == 1
    items = client.get(path + '/items', headers=headers).json()['data']['items']
    detail = client.get(path + '/items/' + items[0]['id'], headers=headers)
    assert detail.json()['data']['a']['answer'] == '답변 [REDACTED]'
    assert detail.json()['data']['a']['judgeError']['code'] == 'JUDGE_NOT_CONFIGURED'
    assert 'test-only-secret-token' not in detail.text
    assert all(value == 'NOT_COMPARABLE' for value in items[0]['changes'].values())
    with factory() as db:
        assert db.scalar(select(Evaluation)).secret_ciphertext is None


def test_real_judgement_boundary_and_improvement_filter(ready):
    factory, client, headers, service, body, _ = ready
    judge = TestJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)
    evidence = {'documents': [{'id': 'refund-v2-section-3', 'text': '출발 24시간 전까지 취소할 수 있습니다.'}],
                'citations': [{'documentId': 'refund-v2-section-3', 'quote': '출발 24시간 전까지'}]}

    async def sender(config, payload, outgoing):
        return ChatbotReply(('B:' if payload['messages'][0]['content'] == 'B prompt' else 'A:') + '출발 24시간 전까지', evidence)

    asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    summary = client.get(path + '/summary', headers=headers).json()['data']
    for key in CRITERIA['definitions']:
        result = summary['criteria'][key]
        assert result['a']['passRate'] == 0 and result['b']['passRate'] == 100
        assert result['comparison']['IMPROVED'] == 1 and result['comparison']['pairedDenominator'] == 1
    params = {'criterion': 'policy', 'change': 'IMPROVED'}
    items = client.get(path + '/items', headers=headers, params=params).json()['data']['items']
    assert len(items) == 1
    assert client.get(path + '/items', headers=headers, params={**params, 'change': 'WORSENED'}).json()['data']['items'] == []
    assert client.get(path + '/items', headers=headers, params={'change': 'IMPROVED'}).status_code == 400
    detail = client.get(path + '/items/' + items[0]['id'], headers=headers).json()['data']
    assert detail['b']['evidence']['documents'][0]['id'] == 'refund-v2-section-3'
    assert detail['b']['criteria']['policy']['evidence'] == ['출발 24시간 전까지']


def test_partial_call_failure_and_judge_failure_are_separate(ready):
    factory, client, headers, service, body, _ = ready
    judge = TestJudge()
    app.dependency_overrides[get_evaluation_judge] = lambda: judge
    path = submit(client, headers, service, body)

    async def sender(config, payload, outgoing):
        if payload['messages'][0]['content'] == 'A prompt':
            raise GatewayError('UPSTREAM_AUTH_FAILED', '기업 챗봇 인증 실패')
        return 'B: 출발 24시간 전까지'

    asyncio.run(worker.process_one(factory, sender=sender, judge=judge))
    summary = client.get(path + '/summary', headers=headers).json()['data']
    assert summary['evaluation']['status'] == 'PARTIAL_FAILED'
    assert summary['evaluation']['failedQuestions'] == 1
    assert summary['criteria']['policy']['a']['callFailed'] == 1
    assert summary['criteria']['policy']['a']['denominator'] == 0
    assert summary['criteria']['policy']['b']['passRate'] == 100
    assert summary['criteria']['retrieval']['b']['notEvaluable'] == 1
    assert summary['criteria']['policy']['comparison']['pairedDenominator'] == 0


def test_lease_expiry_preserves_saved_side_and_never_replays(ready):
    factory, client, headers, service, body, _ = ready
    path = submit(client, headers, service, body)
    with factory() as db:
        row = db.scalar(select(Evaluation))
        row.status = 'RUNNING'
        row.lease_token = 'old-lease'
        row.heartbeat_at = now() - timedelta(seconds=worker.LEASE_SECONDS + 1)
        item = db.scalar(select(EvaluationItem))
        item.status = 'RUNNING'
        item.a = {'callStatus': 'SUCCEEDED', 'answer': '保存済み', 'criteria': worker.ungraded('미연결')}
        db.commit()

    async def forbidden(*args):
        pytest.fail('An uncertain external call must not be replayed')

    assert not asyncio.run(worker.process_one(factory, sender=forbidden))
    assert client.get(path, headers=headers).json()['data']['status'] == 'PARTIAL_FAILED'
    with factory() as db:
        item = db.scalar(select(EvaluationItem))
        assert item.a['answer'] == '保存済み'
        assert item.b['error']['code'] == 'WORKER_INTERRUPTED'
        assert worker.locked_run(db, item.evaluation_id, 'old-lease') is None
        assert db.scalar(select(Evaluation)).secret_ciphertext is None


def test_draft_rejected_judge_change_and_idempotency_expiry(ready):
    factory, client, headers, service, body, version_path = ready
    clone = client.post(version_path.rsplit('/versions/', 1)[0] + '/versions', headers=headers,
        json={'sourceVersionId': body['testSetVersionId'], 'expectedRevision': 1})
    assert clone.status_code == 201, clone.text
    response = client.post(service + '/evaluations', headers={**headers, 'Idempotency-Key': 'draft'},
        json={**body, 'testSetVersionId': clone.json()['data']['id']})
    assert response.status_code == 409
    path = submit(client, headers, service, body)
    with factory() as db:
        row = db.scalar(select(Evaluation))
        row.created_at = now() - timedelta(hours=25)
        db.commit()
    assert submit(client, headers, service, body) == path  # queued keys do not expire
    asyncio.run(worker.process_one(factory, judge=TestJudge(), sender=plain_answer))
    assert client.get(path, headers=headers).json()['data']['status'] == 'FAILED'
    assert submit(client, headers, service, body) != path
    listing = client.get(service + '/evaluations', headers=headers, params={'limit': 1}).json()['data']
    assert listing['nextCursor']
    assert len(client.get(service + '/evaluations', headers=headers, params={'cursor': listing['nextCursor']}).json()['data']['items']) == 1
    assert client.get(path + '/items', headers=headers, params={'cursor': 'broken'}).status_code == 400


@pytest.mark.parametrize('mode', ['missing-evidence', 'unknown-quote', 'timeout', 'exception'])
def test_judge_invalid_output_is_never_counted_as_a_pass(mode, monkeypatch):
    class Broken(TestJudge):
        async def judge(self, **kwargs):
            if mode == 'timeout':
                await asyncio.sleep(1)
            if mode == 'exception':
                raise ValueError('private-token')
            output = await super().judge(answer='B: result')
            output['policy']['evidence'] = [] if mode == 'missing-evidence' else ['no such source']
            return output

    monkeypatch.setattr(worker, 'JUDGE_TIMEOUT_SECONDS', 0.01)
    inputs = {'judge': Broken().descriptor(), 'criteria': CRITERIA,
              'testSet': {'policies': [{'content': '출발 24시간 전까지'}]}}
    result, error = asyncio.run(worker.judge_response(Broken(), inputs, {}, 'B: result', {'documents': []}))
    assert all(r['status'] == 'NOT_EVALUABLE' for r in result.values())
    assert error['code'] == ('JUDGE_TIMEOUT' if mode == 'timeout' else 'INVALID_JUDGE_OUTPUT')
    assert 'private-token' not in str(error)


def test_pass_rate_excludes_unknowns_failures_and_unpaired_questions():
    def side(status):
        if status == 'NOT_RUN':
            return None
        result = worker.failed_side('error', 'call failed')
        if status != 'CALL_FAILED':
            result.update(callStatus='SUCCEEDED', criteria={k: {'status': status} for k in CRITERIA['definitions']})
        return result

    rows = [SimpleNamespace(a=side(a), b=side(b)) for a, b in [
        ('PASS', 'PASS'), ('FAIL', 'PASS'), ('NOT_EVALUABLE', 'FAIL'), ('CALL_FAILED', 'PASS'), ('NOT_RUN', 'NOT_RUN')]]
    run = SimpleNamespace(id='id', service_id='s', status='RUNNING', total_questions=5,
        completed_questions=4, failed_questions=1, inputs={'judge': {}, 'comparisonAssurance': {}}, created_at=now(), finished_at=None)
    result = aggregate(run, rows)['criteria']['policy']
    assert result['a']['passRate'] == 50 and result['a']['denominator'] == 2
    assert result['b']['passRate'] == 75 and result['b']['denominator'] == 4
    assert result['comparison']['aPairedPassRate'] == 50
    assert result['comparison']['bPairedPassRate'] == 100
    assert result['comparison']['pairedDenominator'] == 2


@pytest.mark.parametrize('evidence', [
    {'documents': [{'id': 'same', 'text': 'A'}, {'id': 'same', 'text': 'B'}]},
    {'documents': [{'id': 'd', 'text': '   '}]},
    {'documents': [], 'citations': [{'documentId': 'd', 'quote': 'A'}]},
    {'documents': [{'id': 'd', 'text': 'original'}], 'citations': [{'documentId': 'd', 'quote': 'invented'}]},
    {'citations': [{'documentId': 'd', 'quote': 'A'}]},
])
def test_retrieval_contract_rejects_invalid_sources(evidence):
    with pytest.raises(ValueError):
        Evidence.model_validate(evidence)


def test_shutdown_finishes_current_question_and_marks_remaining(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, body, _ = prepare(factory, client, monkeypatch, question_count=2)
    path = submit(client, headers, service, body)
    calls = []

    async def scenario():
        stop = asyncio.Event()
        async def sender(config, payload, outgoing):
            calls.append(payload)
            stop.set()
            return '완료한 답변'
        return await worker.process_one(factory, sender=sender, stop_event=stop)

    assert asyncio.run(scenario())
    assert len(calls) == 2  # both A and B for the current question finish
    data = client.get(path, headers=headers).json()['data']
    assert data['status'] == 'PARTIAL_FAILED'
    assert data['completedQuestions'] == 2 and data['failedQuestions'] == 1
    with factory() as db:
        items = db.scalars(select(EvaluationItem)).all()
        stopped = next(item for item in items if item.status == 'FAILED')
        assert stopped.a['error']['code'] == 'WORKER_STOPPED'
        assert stopped.b['error']['code'] == 'WORKER_STOPPED'


def test_question_failure_does_not_stop_later_questions(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, body, _ = prepare(factory, client, monkeypatch, question_count=2)
    path = submit(client, headers, service, body)
    calls = []
    async def sender(config, payload, outgoing):
        calls.append(payload)
        if len(calls) == 1:
            raise TimeoutError()
        return '완료한 답변'
    asyncio.run(worker.process_one(factory, sender=sender))
    assert len(calls) == 4
    data = client.get(path, headers=headers).json()['data']
    assert data['status'] == 'PARTIAL_FAILED' and data['completedQuestions'] == 2 and data['failedQuestions'] == 1
    page = client.get(path + '/items', headers=headers, params={'limit': 1}).json()['data']
    assert page['nextCursor']
    next_page = client.get(path + '/items', headers=headers, params={'cursor': page['nextCursor'], 'limit': 1}).json()['data']
    assert next_page['nextCursor'] is None and next_page['items'][0]['id'] != page['items'][0]['id']
