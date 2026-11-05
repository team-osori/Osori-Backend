import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, Db, OwnedService, ResourceId
from app.api.policies import Cursor, Limit
from app.core.errors import ApiError
from app.models.evaluation import Evaluation, EvaluationItem
from app.schemas.evaluation import (Criterion, EvaluationCreate, EvaluationInputs, EvaluationItemResponse,
    EvaluationItemSummary, EvaluationResponse, EvaluationSummary)
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.services import evaluations as service
from app.services.auth import now
from app.services.evaluation_judge import EvaluationJudge, get_evaluation_judge
from app.services.policy import paginate, uid

router = APIRouter(prefix='/api/projects/{projectId}/services/{serviceId}/evaluations',
    tags=['evaluations'], responses=ERROR_RESPONSES)


@router.post('', status_code=202, response_model=Data[EvaluationResponse])
def create_evaluation(body: EvaluationCreate, service_row: OwnedService, user: CurrentUser, db: Db,
                      response: Response, judge: Annotated[EvaluationJudge, Depends(get_evaluation_judge)],
                      idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=100, pattern=r'^[!-~]+$')]):
    """Submit one durable A/B run; snapshot both source data and encrypted credentials atomically."""
    key = hashlib.sha256(f'{user.id}|{idempotency_key}'.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude_none=True), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(Evaluation).where(Evaluation.service_id == service_row.id, Evaluation.idempotency_hash == key))
    if existing is not None and existing.status in service.TERMINAL and existing.created_at.replace(tzinfo=None) < (now() - timedelta(hours=24)).replace(tzinfo=None):
        existing.idempotency_hash = None
        db.commit()
        existing = None
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 키에 다른 요청을 사용할 수 없습니다.')
        row = existing
    else:
        inputs, ciphertext = service.build_inputs(db, service_row, body, judge)
        row = Evaluation(id=uid(), service_id=service_row.id, idempotency_hash=key, request_hash=fingerprint,
            inputs=inputs, secret_ciphertext=ciphertext, status='QUEUED', total_questions=len(inputs['testSet']['questions']),
            completed_questions=0, failed_questions=0, created_at=now())
        db.add(row)
        try:
            db.flush()
            for question in inputs['testSet']['questions']:
                db.add(EvaluationItem(id=uid(), evaluation_id=row.id, question_id=question['id'],
                    input=deepcopy(question), status='QUEUED', created_at=now()))
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(select(Evaluation).where(Evaluation.service_id == service_row.id, Evaluation.idempotency_hash == key))
            if row is None:
                raise
            if row.request_hash != fingerprint:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 키에 다른 요청을 사용할 수 없습니다.') from None
    db.refresh(row)
    response.headers['Location'] = f'/api/projects/{service_row.project_id}/services/{service_row.id}/evaluations/{row.id}'
    response.headers['Retry-After'] = '2'
    return {'data': service.run_data(row)}


@router.get('', response_model=Data[Page[EvaluationResponse]])
def list_evaluations(service_row: OwnedService, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """List execution progress without loading large answer payloads."""
    rows, next_cursor = paginate(db, select(Evaluation).where(Evaluation.service_id == service_row.id),
        Evaluation, f'evaluations:{service_row.id}', limit, cursor)
    return {'data': {'items': [service.run_data(row) for row in rows], 'nextCursor': next_cursor}}


@router.get('/{evaluationId}', response_model=Data[EvaluationResponse])
def get_evaluation(evaluationId: ResourceId, service_row: OwnedService, db: Db):
    """Expose durable progress for a run within the caller's service."""
    return {'data': service.run_data(service.get_run(db, service_row.id, evaluationId))}


@router.get('/{evaluationId}/inputs', response_model=Data[EvaluationInputs])
def get_inputs(evaluationId: ResourceId, service_row: OwnedService, db: Db):
    """Expose frozen policy, test-set, prompt and configuration inputs without any credential field."""
    return {'data': service.get_run(db, service_row.id, evaluationId).inputs}


@router.get('/{evaluationId}/summary', response_model=Data[EvaluationSummary])
def get_summary(evaluationId: ResourceId, service_row: OwnedService, db: Db):
    """Keep failures, ungraded cases and paired comparisons separate from judged pass rates."""
    row = service.get_run(db, service_row.id, evaluationId)
    items = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == row.id)).all()
    return {'data': service.aggregate(row, items)}


@router.get('/{evaluationId}/items', response_model=Data[Page[EvaluationItemSummary]])
def list_items(evaluationId: ResourceId, service_row: OwnedService, db: Db,
               limit: Limit = 20, cursor: Cursor = None, criterion: Criterion | None = None,
               change: Annotated[str | None, Query(pattern=r'^(IMPROVED|WORSENED|UNCHANGED_PASS|UNCHANGED_FAIL|NOT_COMPARABLE)$')] = None):
    """Page through per-question improvements/regressions without returning all answers at once."""
    row = service.get_run(db, service_row.id, evaluationId)
    if change and criterion is None:
        raise ApiError(400, 'VALIDATION_ERROR', 'change 필터에는 criterion이 필요합니다.')
    statement = select(EvaluationItem).where(EvaluationItem.evaluation_id == row.id)
    if change:
        # Bounded to 200 items by confirmed test-set constraints; filter IDs before paging.
        items = db.scalars(statement).all()
        ids = [item.id for item in items if service.change(item.a, item.b, criterion) == change]
        statement = statement.where(EvaluationItem.id.in_(ids))
    scope = f'evaluationItems:{row.id}:{criterion}:{change}'
    rows, next_cursor = paginate(db, statement, EvaluationItem, scope, limit, cursor)
    return {'data': {'items': [service.item_summary(item, row.inputs) for item in rows], 'nextCursor': next_cursor}}


@router.get('/{evaluationId}/items/{itemId}', response_model=Data[EvaluationItemResponse])
def get_item(evaluationId: ResourceId, itemId: ResourceId, service_row: OwnedService, db: Db):
    """Return both answers, retrieval evidence, per-criterion reasons and original question input."""
    row = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == row.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    return {'data': {**service.item_summary(item, row.inputs), 'input': item.input, 'a': item.a, 'b': item.b}}
