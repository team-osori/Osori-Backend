import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, Db, OwnedService, ResourceId
from app.api.policies import Cursor, Limit
from app.core.errors import ApiError
from app.models.evaluation import Evaluation, EvaluationItem, EvaluationReview
from app.schemas.evaluation import (Criterion, EvaluationCreate, EvaluationInputs, EvaluationItemResponse,
    EvaluationItemSummary, EvaluationResponse, EvaluationSummary, EvaluationRerun)
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.services import evaluations as service
from app.services import evaluation_analysis as analysis
from app.schemas.condition_evaluation import IssueKind
from app.schemas.condition_evaluation import ReviewCreate, ReviewOut
from app.services.evaluation_reviews import latest_reviews, review_data
from app.services.evaluation_comparison import compare
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
    reviews = latest_reviews(db, [item.id for item in items])
    return {'data': {**service.aggregate(row, items), 'analysis': analysis.summarize(row, items, reviews)}}


@router.get('/{evaluationId}/items', response_model=Data[Page[EvaluationItemSummary]])
def list_items(evaluationId: ResourceId, service_row: OwnedService, db: Db,
               limit: Limit = 20, cursor: Cursor = None, criterion: Criterion | None = None,
               resultStatus: Annotated[str | None, Query(pattern=r'^(MET|UNMET|REVIEW_REQUIRED|JUDGMENT_ERROR|EXECUTION_ERROR|NOT_RUN|IN_PROGRESS)$')] = None,
               issueKind: IssueKind | None = None,
               change: Annotated[str | None, Query(pattern=r'^(IMPROVED|WORSENED|UNCHANGED_PASS|UNCHANGED_FAIL|NOT_COMPARABLE)$')] = None):
    """Page through per-question improvements/regressions without returning all answers at once."""
    row = service.get_run(db, service_row.id, evaluationId)
    if change and criterion is None:
        raise ApiError(400, 'VALIDATION_ERROR', 'change 필터에는 criterion이 필요합니다.')
    if criterion and criterion not in row.inputs['criteria']['definitions']:
        raise ApiError(400, 'INVALID_CRITERION', '이 평가에 포함된 채점 항목을 선택하세요.')
    if change and row.inputs.get('mode') == 'SINGLE':
        raise ApiError(400, 'COMPARISON_REQUIRED', '전후 비교는 comparison API를 사용하세요.')
    statement = select(EvaluationItem).where(EvaluationItem.evaluation_id == row.id)
    if change or resultStatus or issueKind:
        # Bounded to 200 items by confirmed test-set constraints; filter IDs before paging.
        items = db.scalars(statement).all()
        ids = []
        filtered_reviews = latest_reviews(db, [item.id for item in items])
        for item in items:
            detail = analysis.item_analysis(row, item, filtered_reviews)['a']
            if change and service.change(item.a, item.b, criterion) != change:
                continue
            if resultStatus and detail['status'] != resultStatus:
                continue
            if issueKind and not any(issue['kind'] == issueKind for issue in detail['issues']):
                continue
            ids.append(item.id)
        statement = statement.where(EvaluationItem.id.in_(ids))
    scope = f'evaluationItems:{row.id}:{criterion}:{change}:{resultStatus}:{issueKind}'
    rows, next_cursor = paginate(db, statement, EvaluationItem, scope, limit, cursor)
    reviews = latest_reviews(db, [item.id for item in rows])
    return {'data': {'items': [{**service.item_summary(item, row.inputs),
        'resultStatus': analysis.item_analysis(row, item, reviews)['a']['status'],
        'needsReview': analysis.item_analysis(row, item, reviews)['a']['needsReview']} for item in rows], 'nextCursor': next_cursor}}


@router.get('/{evaluationId}/items/{itemId}', response_model=Data[EvaluationItemResponse])
def get_item(evaluationId: ResourceId, itemId: ResourceId, service_row: OwnedService, db: Db):
    """Return both answers, retrieval evidence, per-criterion reasons and original question input."""
    row = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == row.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    reviews = latest_reviews(db, [item.id])
    details = analysis.item_analysis(row, item, reviews)
    return {'data': {**service.item_summary(item, row.inputs), 'input': item.input, 'a': item.a, 'b': item.b,
        'resultStatus': details['a']['status'], 'needsReview': details['a']['needsReview'], 'analysis': details,
        'reviewRevision': item.review_revision, 'latestReviews': list(reviews.values())}}


@router.post('/{evaluationId}/items/{itemId}/reviews', status_code=201, response_model=Data[ReviewOut])
def review_item(evaluationId: ResourceId, itemId: ResourceId, body: ReviewCreate,
                service_row: OwnedService, user: CurrentUser, db: Db):
    run = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == run.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    if run.status not in service.TERMINAL:
        raise ApiError(409, 'EVALUATION_NOT_TERMINAL', '평가가 종료된 뒤 검토하세요.')
    if body.side not in service.side_names(run.inputs):
        raise ApiError(400, 'INVALID_REVIEW_TARGET', '평가한 대상만 검토할 수 있습니다.')
    side = getattr(item, body.side)
    if side is None or side['callStatus'] != 'SUCCEEDED':
        raise ApiError(409, 'ANSWER_UNAVAILABLE', '답변을 확보한 문항만 판정할 수 있습니다.')
    if body.conditionId not in {condition['id'] for condition in item.input.get('expectedConditions', [])}:
        raise ApiError(400, 'INVALID_CONDITION', '해당 문항의 조건을 선택하세요.')
    result = db.execute(update(EvaluationItem).where(EvaluationItem.id == item.id,
        EvaluationItem.review_revision == body.expectedRevision).values(review_revision=EvaluationItem.review_revision + 1)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 검토 revision을 확인하세요.')
    review = EvaluationReview(id=uid(), item_id=item.id, revision=body.expectedRevision + 1,
        side=body.side, condition_id=body.conditionId, verdict=body.verdict, reason=body.reason,
        reviewed_by=user.id, created_at=now())
    db.add(review)
    db.commit()
    return {'data': review_data(review)}


@router.get('/{evaluationId}/items/{itemId}/reviews', response_model=Data[Page[ReviewOut]])
def list_reviews(evaluationId: ResourceId, itemId: ResourceId, service_row: OwnedService, db: Db,
                 limit: Limit = 20, cursor: Cursor = None):
    run = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem.id).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == run.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    rows, next_cursor = paginate(db, select(EvaluationReview).where(EvaluationReview.item_id == itemId),
        EvaluationReview, f'evaluationReviews:{itemId}:revision', limit, cursor,
        versioned=True, sequence=EvaluationReview.revision)
    return {'data': {'items': [review_data(review) for review in rows], 'nextCursor': next_cursor}}


@router.post('/{evaluationId}/reruns', status_code=202, response_model=Data[EvaluationResponse])
def rerun_evaluation(evaluationId: ResourceId, body: EvaluationRerun, service_row: OwnedService,
                     user: CurrentUser, db: Db, response: Response,
                     judge: Annotated[EvaluationJudge, Depends(get_evaluation_judge)],
                     idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=100, pattern=r'^[!-~]+$')]):
    source = service.get_run(db, service_row.id, evaluationId)
    prompt_id = body.promptVersionId
    if 'promptVersionId' not in body.model_fields_set:
        prompt_id = source.inputs.get('prompts', {}).get('a', {}).get('id')
    request = EvaluationCreate(mode='SINGLE', connectionRevision=body.connectionRevision,
        testSetVersionId=source.inputs['testSet']['testSetVersionId'],
        promptVersionId=prompt_id, sourceEvaluationId=source.id)
    return create_evaluation(request, service_row, user, db, response, judge, idempotency_key)


@router.get('/{evaluationId}/comparison', response_model=Data[dict])
def compare_evaluations(evaluationId: ResourceId, service_row: OwnedService, db: Db,
                        baselineEvaluationId: ResourceId | None = None, limit: Limit = 20, cursor: Cursor = None):
    target = service.get_run(db, service_row.id, evaluationId)
    baseline_id = baselineEvaluationId or target.inputs.get('sourceEvaluationId')
    if not baseline_id:
        raise ApiError(400, 'BASELINE_REQUIRED', '비교할 이전 평가를 선택하세요.')
    baseline = service.get_run(db, service_row.id, baseline_id)
    before = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == baseline.id)).all()
    after = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == target.id)).all()
    reviews = latest_reviews(db, [item.id for item in before + after])
    result = compare(baseline, target, before, after, reviews)
    review_hash = hashlib.sha256(json.dumps(sorted((review['id'], review['revision']) for review in reviews.values())).encode()).hexdigest()
    rows, next_cursor = paginate(db, select(EvaluationItem).where(EvaluationItem.evaluation_id == target.id),
        EvaluationItem, f'comparison:{baseline.id}:{target.id}:{review_hash}', limit, cursor)
    by_question = {item['questionId']: item for item in result['items']}
    result['items'] = [by_question[item.question_id] for item in rows]
    result['nextCursor'] = next_cursor
    return {'data': result}
