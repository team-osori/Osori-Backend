import hashlib
import json
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.dependencies import IfMatch, CurrentUser, Db, OwnedService, ResourceId
from app.api.projects import iso
from app.core.errors import ApiError
from app.core.concurrency import is_write_conflict
from app.core.secrets import pack_secrets, unpack_secrets
from app.models.connection import Connection, ConnectionTest
from app.schemas.connection import (ConnectionPut, ConnectionResponse, ConnectionTestCreate,
    ConnectionTestResponse, LlmSecretResponse, SecretPut, SecretResponse)
from app.schemas.responses import Data, ERROR_RESPONSES
from app.services.auth import now
from app.services.chatbot_gateway import validate_endpoint
from app.services.prompts import get_version
from app.services.project_listing import latest_connection_tests

router = APIRouter(prefix='/api/projects/{projectId}/services/{serviceId}',
                   tags=['connections'], responses=ERROR_RESPONSES)


def get_connection(db, service_id):
    connection = db.scalar(select(Connection).where(Connection.service_id == service_id))
    if connection is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '연동 설정을 찾을 수 없습니다.')
    return connection


def connection_data(c):
    auth_secret, llm_secret = unpack_secrets(c.secret_ciphertext)
    return {**c.config, 'id': c.id, 'serviceId': c.service_id, 'revision': c.revision,
        'auth': {**c.config['auth'], 'secretConfigured': auth_secret is not None},
        'llmKeyConfigured': llm_secret is not None,
        'verifiedRevision': c.verified_revision, 'createdAt': iso(c.created_at), 'updatedAt': iso(c.updated_at)}


def test_data(job):
    return {'id': job.id, 'connectionRevision': job.connection_revision, 'status': job.status,
        'answer': job.answer, 'latencyMs': job.latency_ms, 'attemptCount': job.attempt_count,
        'appliedVersionVerified': job.applied_version_verified, 'error': job.error,
        'createdAt': iso(job.created_at), 'finishedAt': iso(job.finished_at) if job.finished_at else None}


def change_connection(db, connection, expected, **values):
    if connection.revision != expected:
        raise ApiError(409, 'REVISION_CONFLICT', '연동 설정이 변경되었습니다. 다시 조회해 주세요.')
    try:
        result = db.execute(update(Connection).where(Connection.id == connection.id,
            Connection.revision == expected).values(**values, revision=Connection.revision + 1,
            verified_revision=None, updated_at=now()))
    except OperationalError as exc:
        db.rollback()
        if is_write_conflict(exc):
            raise ApiError(409, 'REVISION_CONFLICT', '다른 요청이 설정을 변경했습니다. 다시 조회해 주세요.') from None
        raise
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '연동 설정이 변경되었습니다. 다시 조회해 주세요.')
    db.commit()
    db.refresh(connection)


@router.get('/connection', response_model=Data[ConnectionResponse])
def read_connection(service: OwnedService, db: Db):
    return {'data': connection_data(get_connection(db, service.id))}


@router.put('/connection', response_model=Data[ConnectionResponse])
def put_connection(body: ConnectionPut, service: OwnedService, db: Db):
    validate_endpoint(body.url)
    config = body.model_dump(exclude={'expectedRevision'})
    # 지정하지 않았으면 키를 아예 두지 않아, 이전 평가의 설정과 비교할 때 달라 보이지 않게 함
    if config.get('llmKeyHeaderName') is None:
        config.pop('llmKeyHeaderName', None)
    c = db.scalar(select(Connection).where(Connection.service_id == service.id))
    if c is None:
        if body.expectedRevision != 0:
            raise ApiError(409, 'REVISION_CONFLICT', '최초 저장에는 expectedRevision=0이 필요합니다.')
        c = Connection(id=str(uuid4()), service_id=service.id, revision=1, config=config,
                       created_at=now(), updated_at=now())
        db.add(c)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, 'REVISION_CONFLICT', '연동 설정이 먼저 등록되었습니다.') from None
        db.refresh(c)
    else:
        # Never forward old credentials to a newly configured endpoint, auth scheme or AI key header.
        auth_secret, llm_secret = unpack_secrets(c.secret_ciphertext)
        same_url = c.config['url'] == config['url']
        keep_auth = same_url and c.config['auth'] == config['auth']
        keep_llm = same_url and c.config.get('llmKeyHeaderName') == config.get('llmKeyHeaderName') and config.get('llmKeyHeaderName')
        change_connection(db, c, body.expectedRevision, config=config,
                          secret_ciphertext=pack_secrets(auth_secret if keep_auth else None, llm_secret if keep_llm else None))
    return {'data': connection_data(c)}


@router.put('/connection/secret', response_model=Data[SecretResponse])
def put_secret(body: SecretPut, service: OwnedService, db: Db):
    c = get_connection(db, service.id)
    if c.config['auth']['type'] == 'NONE':
        raise ApiError(409, 'AUTH_NOT_REQUIRED', '인증 없는 연결에는 비밀값을 등록할 수 없습니다.')
    _, llm_secret = unpack_secrets(c.secret_ciphertext)
    change_connection(db, c, body.expectedRevision,
                      secret_ciphertext=pack_secrets(body.secret.get_secret_value(), llm_secret))
    return {'data': {'connectionId': c.id, 'revision': c.revision,
                     'secretConfigured': True, 'verifiedRevision': None}}


def if_match_revision(if_match):
    import re
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if not re.fullmatch(r'"[1-9][0-9]*"', if_match):
        raise ApiError(400, 'VALIDATION_ERROR', 'If-Match는 큰따옴표로 감싼 revision이어야 합니다.')
    return int(if_match[1:-1])


def change_with_precondition(db, c, expected, **values):
    try:
        change_connection(db, c, expected, **values)
    except ApiError as exc:
        if exc.code == 'REVISION_CONFLICT':
            raise ApiError(412, 'PRECONDITION_FAILED', '연동 설정이 변경되었습니다.') from None
        raise


@router.delete('/connection/secret', status_code=204)
def delete_secret(service: OwnedService, db: Db,
                  if_match: IfMatch = None):
    c = get_connection(db, service.id)
    expected = if_match_revision(if_match)
    _, llm_secret = unpack_secrets(c.secret_ciphertext)
    change_with_precondition(db, c, expected, secret_ciphertext=pack_secrets(None, llm_secret))
    return Response(status_code=204)


@router.put('/connection/llm-secret', response_model=Data[LlmSecretResponse],
            description='평가 대상 서비스가 답변 생성에 쓰는 AI API 키를 등록·교체합니다. '
                        '연결 설정의 llmKeyHeaderName 헤더로 매 호출에 전달하며, 응답에 키 값은 돌려주지 않습니다.')
def put_llm_secret(body: SecretPut, service: OwnedService, db: Db):
    c = get_connection(db, service.id)
    if not c.config.get('llmKeyHeaderName'):
        raise ApiError(409, 'LLM_KEY_HEADER_REQUIRED', 'AI API 키를 보낼 헤더 이름을 먼저 연결 설정에 저장해 주세요.')
    auth_secret, _ = unpack_secrets(c.secret_ciphertext)
    change_connection(db, c, body.expectedRevision,
                      secret_ciphertext=pack_secrets(auth_secret, body.secret.get_secret_value()))
    return {'data': {'connectionId': c.id, 'revision': c.revision,
                     'llmKeyConfigured': True, 'verifiedRevision': None}}


@router.delete('/connection/llm-secret', status_code=204)
def delete_llm_secret(service: OwnedService, db: Db, if_match: IfMatch = None):
    c = get_connection(db, service.id)
    expected = if_match_revision(if_match)
    auth_secret, _ = unpack_secrets(c.secret_ciphertext)
    change_with_precondition(db, c, expected, secret_ciphertext=pack_secrets(auth_secret, None))
    return Response(status_code=204)


@router.post('/connection-tests', status_code=202, response_model=Data[ConnectionTestResponse])
def create_test(body: ConnectionTestCreate, service: OwnedService, user: CurrentUser, db: Db,
                response: Response,
                idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1,
                                                       max_length=100, pattern=r'^[!-~]+$')]):
    digest = hashlib.sha256(f'{user.id}:{idempotency_key}'.encode()).hexdigest()
    request_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(ConnectionTest).where(ConnectionTest.service_id == service.id,
                                                      ConnectionTest.idempotency_hash == digest))
    # Release completed keys after 24 hours without deleting result history.
    # Do not UPDATE absent keys: MySQL gap locks could deadlock concurrent first submissions.
    cutoff = now() - timedelta(hours=24)
    if (existing is not None and existing.status in ('SUCCEEDED', 'FAILED')
            and existing.created_at.replace(tzinfo=None) < cutoff.replace(tzinfo=None)):
        db.execute(update(ConnectionTest).where(ConnectionTest.id == existing.id,
            ConnectionTest.idempotency_hash == digest).values(idempotency_hash=None))
        db.commit()
        existing = db.scalar(select(ConnectionTest).where(ConnectionTest.service_id == service.id,
                                                          ConnectionTest.idempotency_hash == digest))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 멱등키에 다른 요청을 사용할 수 없습니다.')
        response.headers['Location'] = f'/api/projects/{service.project_id}/services/{service.id}/connection-tests/{existing.id}'
        return {'data': test_data(existing)}
    c = get_connection(db, service.id)
    if c.revision != body.connectionRevision:
        raise ApiError(409, 'CONNECTION_CHANGED', '연동 설정이 변경되었습니다.')
    if c.config['auth']['type'] != 'NONE' and unpack_secrets(c.secret_ciphertext)[0] is None:
        raise ApiError(409, 'CONNECTION_SECRET_REQUIRED', '인증정보를 먼저 등록해 주세요.')
    if c.config['executionMode'] == 'PROMPT_BODY' and body.promptVersionId is None:
        raise ApiError(400, 'VALIDATION_ERROR', 'promptVersionId가 필요합니다.')
    if body.promptVersionId:
        get_version(db, service.id, body.promptVersionId)
    validate_endpoint(c.config['url'])
    job = ConnectionTest(id=str(uuid4()), service_id=service.id, connection_id=c.id,
        connection_revision=c.revision, idempotency_hash=digest, request_hash=request_hash,
        question=body.question, prompt_version_id=body.promptVersionId, status='QUEUED',
        created_at=now(), attempt_count=0, applied_version_verified=False)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ConnectionTest).where(ConnectionTest.service_id == service.id,
                                                         ConnectionTest.idempotency_hash == digest))
        if existing is None:
            raise
        if existing.request_hash != request_hash:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 멱등키에 다른 요청을 사용할 수 없습니다.') from None
        job = existing
    db.refresh(job)
    response.headers['Location'] = f'/api/projects/{service.project_id}/services/{service.id}/connection-tests/{job.id}'
    return {'data': test_data(job)}


@router.get('/connection-tests/{connectionTestId}', response_model=Data[ConnectionTestResponse])
def read_test(connectionTestId: ResourceId, service: OwnedService, db: Db):
    job = db.scalar(select(ConnectionTest).where(ConnectionTest.id == connectionTestId,
                                               ConnectionTest.service_id == service.id))
    if job is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '연결 테스트를 찾을 수 없습니다.')
    return {'data': test_data(job)}


@router.get('/connection-tests', response_model=Data[ConnectionTestResponse | None])
def read_latest_test(service: OwnedService, db: Db):
    """Recover the latest test after refresh; old revisions are explicitly stale."""
    job = latest_connection_tests(db, [service.id]).get(service.id)
    if job is None:
        return {'data': None}
    connection = get_connection(db, service.id)
    return {'data': {**test_data(job), 'isCurrentRevision': job.connection_revision == connection.revision}}
