import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, Db, OwnedService, ResourceId
from app.api.policies import Cursor, Limit
from app.core.errors import ApiError
from app.models.evaluation import Evaluation, EvaluationItem, EvaluationReview, EvaluationAnswerReview
from app.schemas.evaluation import (Criterion, EvaluationCreate, EvaluationInputs, EvaluationItemResponse,
    EvaluationItemSummary, EvaluationResponse, CauseArea, AnalysisStatus, ScreenStatus, EvaluationSummary, EvaluationRerun, EvaluationComparison)
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.services import evaluations as service
from app.services import evaluation_analysis as analysis
from app.services import evaluation_grading as grading
from app.schemas.grading import GradingBoard
from app.schemas.condition_evaluation import IssueKind
from app.schemas.condition_evaluation import ReviewCreate, ReviewOut, AnswerReviewCreate, AnswerReviewOut
from app.services.evaluation_reviews import latest_reviews, review_data, latest_answer_reviews, answer_review_data
from app.services.evaluation_comparison import compare
from app.services.auth import now
from app.services.evaluation_judge import EvaluationJudge, get_evaluation_judge
from app.services.policy import paginate, uid
from app.schemas.evaluation import EvaluationAction, EvaluationResume, AnalysisCapabilities
from app.services.evaluation_control import cancel_run, resume_run

router = APIRouter(prefix='/api/projects/{projectId}/services/{serviceId}/evaluations',
    tags=['evaluations'], responses=ERROR_RESPONSES)


@router.get('/capabilities', response_model=Data[AnalysisCapabilities])
def capabilities(service_row: OwnedService):
    """지원하지 않는 분석을 0점 또는 가짜 제안으로 표시하지 않도록 기능별 상태를 반환합니다."""
    return {'data': {
        'causeClassification': {'supported': False, 'reason': '검색·프롬프트·모델의 인과 분리 실험을 수행하지 않습니다.'},
        'causeHypotheses': {'supported': True, 'reason': '수집한 근거가 있는 가설만 제공합니다. 확정 원인이 아닙니다.'},
        'promptSuggestions': {'supported': False, 'reason': '조건별 개선 권고는 있지만 프롬프트 자동 수정안은 생성하지 않습니다.'},
        'promptDiff': {'supported': False, 'reason': '생성된 프롬프트 수정안이 없습니다.'},
        'interRaterAgreement': {'supported': False, 'reason': '독립 다중 채점자 실험을 수행하지 않습니다.'},
        'unstableQuestions': {'supported': False, 'reason': '동일 조건의 반복 실행 실험을 수행하지 않습니다.'},
        'fixableQuestionCount': {'supported': False, 'reason': '프롬프트 변경 효과를 사전에 예측하지 않습니다.'},
    }}


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
    return {'data': {**service.aggregate(row, items), 'analysis': analysis.summarize(row, items, reviews,
        latest_answer_reviews(db, [item.id for item in items]))}}


def analyzed_item_summary(run, item, detail):
    return {**service.item_summary(item, run.inputs),
            'resultStatus': detail['status'], 'needsReview': detail['needsReview'],
            'screenStatus': detail['screenStatus'], 'analysisStatus': detail['analysisStatus'],
            'activeHypothesisAreas': detail['activeHypothesisAreas']}


@router.get('/{evaluationId}/grading', response_model=Data[GradingBoard])
def get_grading_board(evaluationId: ResourceId, service_row: OwnedService, db: Db):
    """최대 200개 문항의 고정 번호·상태를 답변 원문 없이 한 번에 조회합니다."""
    return {'data': grading.board(db, service.get_run(db, service_row.id, evaluationId))}


@router.get('/{evaluationId}/items', response_model=Data[Page[EvaluationItemSummary]])
def list_items(evaluationId: ResourceId, service_row: OwnedService, db: Db,
               limit: Limit = 20, cursor: Cursor = None, criterion: Annotated[Criterion | None, Query(description='평가 inputs.criteria.definitions에 포함된 기준만 필터링할 수 있습니다. 미포함 기준은 빈 목록을 반환합니다.')] = None,
               resultStatus: Annotated[str | None, Query(pattern=r'^(MET|UNMET|REVIEW_REQUIRED|JUDGMENT_ERROR|EXECUTION_ERROR|NOT_RUN|IN_PROGRESS)$')] = None,
               issueKind: IssueKind | None = None,
               side: Literal['a', 'b'] = 'a',
               screenStatus: ScreenStatus | None = None,
               causeArea: CauseArea | None = None,
               analysisStatus: AnalysisStatus | None = None,
               change: Annotated[str | None, Query(pattern=r'^(IMPROVED|WORSENED|UNCHANGED_PASS|UNCHANGED_FAIL|NOT_COMPARABLE)$', description='criterion이 필수이며 COMPARE 평가에서만 사용합니다. SINGLE 평가는 comparison API를 사용하세요.')] = None):
    """Page through per-question improvements/regressions without returning all answers at once."""
    row = service.get_run(db, service_row.id, evaluationId)
    if side not in service.side_names(row.inputs):
        raise ApiError(400, 'INVALID_ANALYSIS_SIDE', '평가한 대상만 조회할 수 있습니다.')
    if change and criterion is None:
        raise ApiError(400, 'VALIDATION_ERROR', 'change 필터에는 criterion이 필요합니다.')
    if change and row.inputs.get('mode') == 'SINGLE':
        raise ApiError(400, 'COMPARISON_REQUIRED', '전후 비교는 comparison API를 사용하세요.')
    if criterion and criterion not in row.inputs['criteria']['definitions']:
        return {'data': {'items': [], 'nextCursor': None}}
    statement = select(EvaluationItem).where(EvaluationItem.evaluation_id == row.id)
    if change or resultStatus or issueKind or screenStatus or causeArea or analysisStatus:
        # Bounded to 200 items by confirmed test-set constraints; filter IDs before paging.
        items = db.scalars(statement).all()
        ids = []
        filtered_reviews = latest_reviews(db, [item.id for item in items])
        filtered_answers = latest_answer_reviews(db, [item.id for item in items])
        for item in items:
            detail = analysis.item_analysis(row, item, filtered_reviews, filtered_answers)[side]
            if change and service.change(item.a, item.b, criterion) != change:
                continue
            if resultStatus and detail['status'] != resultStatus:
                continue
            if issueKind and not any(issue['kind'] == issueKind for issue in detail['issues']):
                continue
            if screenStatus and detail['screenStatus'] != screenStatus:
                continue
            if causeArea and causeArea not in detail['activeHypothesisAreas']:
                continue
            if analysisStatus and detail['analysisStatus'] != analysisStatus:
                continue
            ids.append(item.id)
        statement = statement.where(EvaluationItem.id.in_(ids))
    scope = f'evaluationItems:{row.id}:{criterion}:{change}:{resultStatus}:{issueKind}:{side}:{screenStatus}:{causeArea}:{analysisStatus}'
    rows, next_cursor = paginate(db, statement, EvaluationItem, scope, limit, cursor)
    reviews = latest_reviews(db, [item.id for item in rows])
    answer_reviews = latest_answer_reviews(db, [item.id for item in rows])
    return {'data': {'items': [analyzed_item_summary(row, item, analysis.item_analysis(row, item, reviews, answer_reviews)[side])
                              for item in rows], 'nextCursor': next_cursor}}


@router.get('/{evaluationId}/items/{itemId}', response_model=Data[EvaluationItemResponse])
def get_item(evaluationId: ResourceId, itemId: ResourceId, service_row: OwnedService, db: Db):
    """Return both answers, retrieval evidence, per-criterion reasons and original question input."""
    row = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == row.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    reviews = latest_reviews(db, [item.id])
    details = analysis.item_analysis(row, item, reviews, latest_answer_reviews(db, [item.id]))
    return {'data': {**analyzed_item_summary(row, item, details['a']), 'input': item.input, 'a': item.a, 'b': item.b,
        'resultStatus': details['a']['status'], 'needsReview': details['a']['needsReview'], 'analysis': details,
        'reviewRevision': item.review_revision, 'latestReviews': list(reviews.values()),
        **grading.navigation(db, row, item.question_id)}}


@router.post('/{evaluationId}/items/{itemId}/reviews', status_code=201, response_model=Data[ReviewOut])
def review_item(evaluationId: ResourceId, itemId: ResourceId, body: ReviewCreate,
                service_row: OwnedService, user: CurrentUser, db: Db):
    run, item = review_target(db, service_row.id, evaluationId, itemId, body.side)
    if body.conditionId not in {condition['id'] for condition in item.input.get('expectedConditions', [])}:
        raise ApiError(400, 'INVALID_CONDITION', '해당 문항의 조건을 선택하세요.')
    claim_review_revision(db, item, body.expectedRevision)
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


@router.post('/{evaluationId}/reruns', status_code=202, response_model=Data[EvaluationResponse],
    description='종료한 SINGLE v2 평가만 재실행할 수 있습니다(COMPLETED/PARTIAL_FAILED/FAILED). COMPARE 또는 진행 중인 평가는 409 INVALID_REEVALUATION_SOURCE입니다. 원본 질문·기준·판정 모델과 연동 설정을 유지해야 합니다.')
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


@router.get('/{evaluationId}/comparison', response_model=Data[EvaluationComparison])
def compare_evaluations(evaluationId: ResourceId, service_row: OwnedService, db: Db,
                        baselineEvaluationId: Annotated[str | None, Query(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$',
                            description='비교 기준 평가 ID. 생략하면 재실행 원본 sourceEvaluationId를 사용합니다. 원본이 없는 평가에서는 필수이며 없으면 400 BASELINE_REQUIRED입니다.')] = None, limit: Limit = 20, cursor: Cursor = None):
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


def controlled_run(db, service_id, evaluation_id):
    row = db.scalar(select(Evaluation).where(Evaluation.id == evaluation_id,
        Evaluation.service_id == service_id).with_for_update())
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가를 찾을 수 없습니다.')
    return row


@router.post('/{evaluationId}/cancel', response_model=Data[EvaluationResponse])
def cancel_evaluation(evaluationId: ResourceId, body: EvaluationAction, service_row: OwnedService, db: Db):
    """대기·진행 평가를 중단합니다. 저장된 결과는 보존하고 외부에서 이미 시작된 호출은 결과 불확실로 표시합니다."""
    row = controlled_run(db, service_row.id, evaluationId)
    cancel_run(db, row)
    db.commit()
    return {'data': service.run_data(row)}


@router.post('/{evaluationId}/resume', status_code=202, response_model=Data[EvaluationResponse])
def resume_evaluation(evaluationId: ResourceId, body: EvaluationResume, service_row: OwnedService, db: Db,
                      judge: Annotated[EvaluationJudge, Depends(get_evaluation_judge)]):
    """미호출 항목만 재개합니다. 성공·실패·결과 불확실 호출은 반복하지 않으며 기존 설정과 판정 모델이 같아야 합니다."""
    row = controlled_run(db, service_row.id, evaluationId)
    resume_run(db, row, body.connectionRevision, judge)
    db.commit()
    return {'data': service.run_data(row)}



def review_target(db, service_id, evaluation_id, item_id, side_name):
    run = service.get_run(db, service_id, evaluation_id)
    item = db.scalar(select(EvaluationItem).where(EvaluationItem.id == item_id, EvaluationItem.evaluation_id == run.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    if run.status not in service.TERMINAL:
        raise ApiError(409, 'EVALUATION_NOT_TERMINAL', '평가가 종료된 뒤 검토하세요.')
    if side_name not in service.side_names(run.inputs):
        raise ApiError(400, 'INVALID_REVIEW_TARGET', '평가한 대상만 검토할 수 있습니다.')
    side = getattr(item, side_name)
    if side is None or side['callStatus'] != 'SUCCEEDED':
        raise ApiError(409, 'ANSWER_UNAVAILABLE', '답변을 확보한 문항만 판정할 수 있습니다.')
    return run, item


def claim_review_revision(db, item, expected_revision):
    result = db.execute(update(EvaluationItem).where(EvaluationItem.id == item.id,
        EvaluationItem.review_revision == expected_revision).values(review_revision=EvaluationItem.review_revision + 1)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 검토 revision을 확인하세요.')


@router.post('/{evaluationId}/items/{itemId}/answer-reviews', status_code=201, response_model=Data[AnswerReviewOut])
def review_answer(evaluationId: ResourceId, itemId: ResourceId, body: AnswerReviewCreate,
                  service_row: OwnedService, user: CurrentUser, db: Db):
    """챗봇 답변 전체 O/X를 저장합니다. 조건별 판정과 충돌하면 화면은 검토 필요로 표시합니다."""
    run, item = review_target(db, service_row.id, evaluationId, itemId, body.side)
    claim_review_revision(db, item, body.expectedRevision)
    review = EvaluationAnswerReview(id=uid(), item_id=item.id, revision=body.expectedRevision + 1,
        side=body.side, verdict=body.verdict, reason=body.reason, reviewed_by=user.id, created_at=now())
    db.add(review)
    db.commit()
    return {'data': answer_review_data(review)}


@router.get('/{evaluationId}/items/{itemId}/answer-reviews', response_model=Data[Page[AnswerReviewOut]])
def list_answer_reviews(evaluationId: ResourceId, itemId: ResourceId, service_row: OwnedService, db: Db,
                        limit: Limit = 20, cursor: Cursor = None):
    run = service.get_run(db, service_row.id, evaluationId)
    item = db.scalar(select(EvaluationItem.id).where(EvaluationItem.id == itemId, EvaluationItem.evaluation_id == run.id))
    if item is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    rows, next_cursor = paginate(db, select(EvaluationAnswerReview).where(EvaluationAnswerReview.item_id == itemId),
        EvaluationAnswerReview, f'evaluationAnswerReviews:{itemId}:revision', limit, cursor,
        versioned=True, sequence=EvaluationAnswerReview.revision)
    return {'data': {'items': [answer_review_data(review) for review in rows], 'nextCursor': next_cursor}}
