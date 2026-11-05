import hashlib
import json
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.api.dependencies import CurrentUser, Db, OwnedProject, ResourceId
from app.api.policies import Cursor, Limit
from app.core.errors import ApiError
from app.models.policy import GenerationRequest, TestQuestion, TestSet, TestSetVersion
from app.schemas.policy import Generate, GeneratedQuestions, NewDraft, QuestionWrite, Review, Revision, TestSetCreate
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.schemas.policy import TestSetOut, TestSetDetail, TestSetVersionOut, TestSetVersionSummary, QuestionOut, EvaluationSnapshot
from app.services import policy as service
from app.services.auth import now
from app.services.question_generation import QuestionGenerator, get_question_generator

router = APIRouter(prefix='/api/projects/{projectId}/testsets', tags=['testsets'], responses=ERROR_RESPONSES)
MAX_QUESTIONS = 200


def resolve(db, project_id, testset_id, version_id):
    """Check both parent membership and version membership before any read or write."""
    service.testset_row(db, project_id, testset_id)
    return service.version_row(db, testset_id, version_id)


def ensure_capacity(db, version_id, extra):
    """Keep drafts and exported evaluation snapshots within a bounded size."""
    count = len(db.scalars(select(TestQuestion.id).where(TestQuestion.testset_version_id == version_id).with_for_update()).all())
    if count + extra > MAX_QUESTIONS:
        raise ApiError(400, 'QUESTION_LIMIT_EXCEEDED', '테스트셋 버전은 최대 200문항입니다.')


def question_row(db, version_id, question_id):
    """Prevent a question from being accessed through an unrelated version."""
    row = db.scalar(select(TestQuestion).where(TestQuestion.id == question_id, TestQuestion.testset_version_id == version_id).with_for_update())
    if row is None:
        raise service.not_found()
    return row


@router.post('', status_code=201, response_model=Data[TestSetDetail])
def create_testset(body: TestSetCreate, project: OwnedProject, db: Db, response: Response):
    """Create a reviewable draft anchored to explicit immutable policy versions."""
    service.policy_versions(db, project.id, body.policyVersionIds)
    row = TestSet(id=service.uid(), project_id=project.id, name=body.name, revision=1, created_at=now())
    db.add(row)
    db.flush()
    version = TestSetVersion(id=service.uid(), testset_id=row.id, version=1, revision=1, status='draft',
        policy_version_ids=body.policyVersionIds, snapshot=None, created_at=now())
    db.add(version)
    db.commit()
    db.refresh(row)
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{project.id}/testsets/{row.id}/versions/{version.id}'
    return {'data': {'testSet': service.testset_data(row), 'version': service.version_data(db, version)}}


@router.get('', response_model=Data[Page[TestSetOut]])
def list_testsets(project: OwnedProject, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Return only test sets within the caller's authorized project."""
    rows, next_cursor = service.paginate(db, select(TestSet).where(TestSet.project_id == project.id),
        TestSet, f'testsets:{project.id}', limit, cursor)
    return {'data': {'items': [service.testset_data(row) for row in rows], 'nextCursor': next_cursor}}


@router.get('/{testSetId}', response_model=Data[TestSetDetail])
def get_testset(testSetId: ResourceId, project: OwnedProject, db: Db):
    """Return test-set metadata and its latest draft or confirmed version."""
    row = service.testset_row(db, project.id, testSetId)
    version = db.scalar(select(TestSetVersion).where(TestSetVersion.testset_id == row.id).order_by(TestSetVersion.version.desc()))
    return {'data': {'testSet': service.testset_data(row), 'version': service.version_data(db, version)}}


@router.get('/{testSetId}/versions', response_model=Data[Page[TestSetVersionSummary]])
def list_versions(testSetId: ResourceId, project: OwnedProject, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Page through immutable version identities in descending version order."""
    service.testset_row(db, project.id, testSetId)
    rows, next_cursor = service.paginate(db, select(TestSetVersion).where(TestSetVersion.testset_id == testSetId),
        TestSetVersion, f'testsetVersions:{testSetId}', limit, cursor, versioned=True)
    return {'data': {'items': [service.version_summary(row) for row in rows], 'nextCursor': next_cursor}}


@router.get('/{testSetId}/versions/{versionId}', response_model=Data[TestSetVersionOut])
def get_version(testSetId: ResourceId, versionId: ResourceId, project: OwnedProject, db: Db):
    """Expose the review queue without confusing version numbers with version IDs."""
    return {'data': service.version_data(db, resolve(db, project.id, testSetId, versionId))}


@router.post('/{testSetId}/versions', status_code=201, response_model=Data[TestSetVersionOut])
def new_draft(testSetId: ResourceId, body: NewDraft, project: OwnedProject, db: Db, response: Response):
    """Copy a confirmed version to a new draft and require fresh human review."""
    row = service.testset_row(db, project.id, testSetId)
    source = service.version_row(db, testSetId, body.sourceVersionId)
    if source.status != 'confirmed':
        raise ApiError(409, 'TESTSET_NOT_CONFIRMED', '확정된 버전에서 새 초안을 만드세요.')
    if body.policyVersionIds is not None:
        service.policy_versions(db, project.id, body.policyVersionIds)
    result = db.execute(update(TestSet).where(TestSet.id == row.id, TestSet.revision == body.expectedRevision)
        .values(revision=TestSet.revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 테스트셋 revision으로 다시 요청해 주세요.')
    db.refresh(row)
    version = TestSetVersion(id=service.uid(), testset_id=row.id, version=row.revision, revision=1, status='draft',
        policy_version_ids=body.policyVersionIds if body.policyVersionIds is not None else list(source.policy_version_ids), snapshot=None, created_at=now())
    db.add(version)
    db.flush()
    for question in service.questions(db, source.id) if body.policyVersionIds is None else []:
        db.add(TestQuestion(id=service.uid(), testset_version_id=version.id, payload=question.payload,
            review_status='pending', created_at=now()))
    db.commit()
    response.headers['Location'] = f'/api/projects/{project.id}/testsets/{row.id}/versions/{version.id}'
    return {'data': service.version_data(db, version)}


@router.post('/{testSetId}/versions/{versionId}/questions', status_code=201, response_model=Data[TestSetVersionOut])
def add_question(testSetId: ResourceId, versionId: ResourceId, body: QuestionWrite, project: OwnedProject, db: Db, response: Response):
    """Support manual questions while the generation provider is not yet selected."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    ensure_capacity(db, version.id, 1)
    row = TestQuestion(id=service.uid(), testset_version_id=version.id,
        payload=service.question_payload(db, project.id, version, body), review_status='pending', created_at=now())
    db.add(row)
    db.commit()
    response.headers['Location'] = f'/api/projects/{project.id}/testsets/{testSetId}/versions/{version.id}/questions/{row.id}'
    return {'data': service.version_data(db, version)}


@router.get('/{testSetId}/versions/{versionId}/questions/{questionId}', response_model=Data[QuestionOut])
def get_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, project: OwnedProject, db: Db):
    """Return a single question after checking every parent scope."""
    version = resolve(db, project.id, testSetId, versionId)
    return {'data': service.question_data(question_row(db, version.id, questionId))}


@router.patch('/{testSetId}/versions/{versionId}/questions/{questionId}', response_model=Data[TestSetVersionOut])
def edit_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, body: QuestionWrite, project: OwnedProject, db: Db):
    """Replace a draft question and invalidate any prior human approval."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    row = question_row(db, version.id, questionId)
    row.payload = service.question_payload(db, project.id, version, body)
    row.review_status, row.reviewed_by, row.review_note = 'pending', None, None
    db.commit()
    return {'data': service.version_data(db, version)}


@router.post('/{testSetId}/versions/{versionId}/questions/{questionId}/review', response_model=Data[TestSetVersionOut])
def review_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, body: Review,
                    project: OwnedProject, user: CurrentUser, db: Db):
    """Record explicit human approval or rejection; generation never grants approval."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    row = question_row(db, version.id, questionId)
    row.review_status, row.reviewed_by, row.review_note = body.status, user.id, body.note
    db.commit()
    return {'data': service.version_data(db, version)}


@router.delete('/{testSetId}/versions/{versionId}/questions/{questionId}', status_code=204)
def delete_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, project: OwnedProject, db: Db,
                    if_match: Annotated[str | None, Header()] = None):
    """Remove a draft-only question using the current version revision as If-Match."""
    version = resolve(db, project.id, testSetId, versionId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{version.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인해 주세요.')
    service.claim_draft(db, version, version.revision)
    db.delete(question_row(db, version.id, questionId))
    db.commit()
    return Response(status_code=204)


@router.post('/{testSetId}/versions/{versionId}/confirm', response_model=Data[TestSetVersionOut])
def confirm(testSetId: ResourceId, versionId: ResourceId, body: Revision, project: OwnedProject, user: CurrentUser, db: Db):
    """Freeze complete source evidence only when every question is approved by a human."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    rows = service.questions(db, version.id, lock=True)
    if not rows or any(q.review_status != 'approved' or q.reviewed_by is None for q in rows):
        raise ApiError(409, 'REVIEW_REQUIRED', '모든 문항을 사람이 승인한 뒤 확정하세요.')
    sources = service.policy_versions(db, project.id, version.policy_version_ids, allow_deleted=True)
    version.status, version.confirmed_by, version.confirmed_at = 'confirmed', user.id, now()
    version.snapshot = {'schemaVersion': 1, 'projectId': project.id, 'testSetId': testSetId,
        'testSetVersionId': version.id, 'version': version.version, 'confirmedBy': user.id,
        'confirmedAt': service.iso(version.confirmed_at), 'policies': [service.policy_version_data(p) for p in sources],
        'questions': [service.question_data(q) for q in rows]}
    db.commit()
    return {'data': service.version_data(db, version)}


@router.get('/{testSetId}/versions/{versionId}/evaluation-snapshot', response_model=Data[EvaluationSnapshot])
def evaluation_snapshot(testSetId: ResourceId, versionId: ResourceId, project: OwnedProject, db: Db):
    """Supply the evaluation team with an immutable, fully reviewed input snapshot."""
    version = resolve(db, project.id, testSetId, versionId)
    return {'data': service.get_evaluation_snapshot(db, project.id, version.id)}


@router.post('/{testSetId}/versions/{versionId}/questions/generate', response_model=Data[TestSetVersionOut])
def generate(testSetId: ResourceId, versionId: ResourceId, body: Generate, project: OwnedProject,
             user: CurrentUser, db: Db, generator: Annotated[QuestionGenerator, Depends(get_question_generator)],
             idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=100)]):
    """Validate an injected provider's batch and save pending questions exactly once per key."""
    if not idempotency_key.strip():
        raise ApiError(400, 'VALIDATION_ERROR', 'Idempotency-Key는 공백일 수 없습니다.')
    version = resolve(db, project.id, testSetId, versionId)
    # Serialize generation calls for this version before checking replay or revision.
    db.execute(select(TestSetVersion.id).where(TestSetVersion.id == version.id).with_for_update())
    key = hashlib.sha256(f'{user.id}|{version.id}|{idempotency_key}'.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    old = db.scalar(select(GenerationRequest).where(GenerationRequest.key == key).with_for_update())
    if old is not None:
        expiry = old.expires_at.replace(tzinfo=now().tzinfo)
        if expiry > now():
            if old.request_hash != fingerprint:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 키에 다른 요청을 사용할 수 없습니다.')
            return old.response
        db.delete(old)
        db.flush()
    service.claim_draft(db, version, body.expectedRevision)
    ensure_capacity(db, version.id, body.count)
    sources = service.policy_versions(db, project.id, version.policy_version_ids, allow_deleted=True)
    try:
        generated = GeneratedQuestions.model_validate(generator.generate(policies=[service.policy_version_data(p) for p in sources], count=body.count))
        if len(generated.questions) != body.count:
            raise ValueError('Unexpected question count')
        try:
            payloads = [service.question_payload(db, project.id, version, question) for question in generated.questions]
        except ApiError:
            raise ValueError('Invalid generated evidence') from None
    except ApiError:
        db.rollback()
        raise
    except (ValidationError, ValueError):
        db.rollback()
        raise ApiError(502, 'INVALID_GENERATION_OUTPUT', '생성 결과의 형식 또는 근거를 확인할 수 없습니다.') from None
    except TimeoutError:
        db.rollback()
        raise ApiError(504, 'GENERATION_TIMEOUT', '질문 생성 시간이 초과되었습니다.') from None
    except Exception:
        db.rollback()
        raise ApiError(503, 'GENERATION_UNAVAILABLE', '질문 생성 서비스를 사용할 수 없습니다.') from None
    for payload in payloads:
        db.add(TestQuestion(id=service.uid(), testset_version_id=version.id, payload=payload, review_status='pending', created_at=now()))
    db.flush()
    result = {'data': service.version_data(db, version)}
    db.add(GenerationRequest(key=key, request_hash=fingerprint, response=result, expires_at=now() + timedelta(hours=24)))
    db.commit()
    return result
