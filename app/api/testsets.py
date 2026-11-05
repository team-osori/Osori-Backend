import hashlib
import inspect
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.api.dependencies import IfMatch, CurrentUser, Db, OwnedProject, ResourceId
from app.api.policies import Cursor, Limit
from app.core.errors import ApiError, error_response
from app.models.policy import GenerationRequest, QuestionHistory, TestQuestion, TestSet, TestSetVersion
from app.schemas.policy import Generate, GeneratedQuestions, NewDraft, Rename, QuestionWrite, Review, Revision, TestSetCreate
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.schemas.policy import TestSetOut, TestSetDetail, TestSetVersionOut, TestSetVersionSummary, QuestionOut, QuestionHistoryOut, EvaluationSnapshot
from app.services import policy as service
from app.services.auth import now
from app.services.question_generation import (QuestionGenerator, get_question_generator, normalize_question,
    parse_generated_questions)
from app.services.rate_limit import check_rate_limit
from app.services.question_import import CsvValidationError, MAX_CSV_BYTES, parse_questions

router = APIRouter(prefix='/api/projects/{projectId}/testsets', tags=['testsets'], responses=ERROR_RESPONSES)
MAX_QUESTIONS = 200
# Each round asks for at most this many questions per model call. Small calls stay far from
# output-token and timeout limits; later rounds shrink further and only refill what is missing.
GENERATION_ROUNDS = (5, 3, 2, 1, 1, 1)
GENERATION_WORKERS = 4
# Retrying cannot fix these, so they surface immediately instead of after every round.
FATAL_GENERATION_ERRORS = {'GENERATION_NOT_CONFIGURED', 'GENERATION_AUTH_FAILED',
                           'GENERATION_INPUT_TOO_LARGE', 'GENERATION_INVALID_REQUEST'}
DROP_ERRORS = {
    'schema': ('GENERATION_SCHEMA_INVALID', '생성 결과의 형식을 확인할 수 없습니다.'),
    'category': ('GENERATION_CATEGORY_MISMATCH', '생성된 문항 유형이 요청과 다릅니다.'),
    'evidence': ('GENERATION_EVIDENCE_INVALID', '생성된 문항의 정책 참조 또는 원문 근거가 유효하지 않습니다.'),
    'conditions': ('GENERATION_CONDITIONS_INVALID', '생성된 기대 조건을 확인할 수 없습니다.'),
}


def resolve(db, project_id, testset_id, version_id):
    """Check both parent membership and version membership before any read or write."""
    service.testset_row(db, project_id, testset_id)
    return service.version_row(db, testset_id, version_id)


def ensure_capacity(db, version_id, extra):
    """Keep drafts and exported evaluation snapshots within a bounded size."""
    count = len(db.scalars(select(TestQuestion.id).where(TestQuestion.testset_version_id == version_id).with_for_update()).all())
    if count + extra > MAX_QUESTIONS:
        raise ApiError(400, 'QUESTION_LIMIT_EXCEEDED', '테스트셋 버전은 최대 200문항입니다.')


def requested_category_counts(body):
    if body.categoryCounts is not None:
        return {category: count for category, count in body.categoryCounts.items() if count}
    categories = body.categories or ['normal', 'exception', 'boundary'][:min(body.count, 3)]
    counts = {category: 1 for category in categories}
    for index in range(body.count - len(categories)):
        category = categories[index % len(categories)]
        counts[category] += 1
    return counts


def accepts_hints(generator):
    """Older/test adapters take only (policies, count, categories); never pass them extra hints."""
    try:
        params = inspect.signature(generator.generate).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == 'hints' or p.kind is p.VAR_KEYWORD for p in params)


def generation_jobs(targets, accepted, chunk, rule_keys, round_index):
    """Split each category's shortfall into small calls, each focused on a different rule slice."""
    jobs = []
    for category, target in targets.items():
        remaining = target - len(accepted[category])
        parts = -(-remaining // chunk) if remaining > 0 else 0
        keys = rule_keys[round_index % len(rule_keys):] + rule_keys[:round_index % len(rule_keys)] if rule_keys else []
        for index in range(parts):
            size = min(chunk, remaining - index * chunk)
            focus = keys[index::parts] if parts > 1 or round_index else []
            jobs.append((category, size, focus[:25]))
    return jobs


def generate_payloads(db, project_id, version, sources, generator, targets):
    """Collect exactly the requested drafts; drop and refill bad items instead of failing the batch."""
    policies = [service.policy_version_data(p) for p in sources]
    rule_keys = [rule['key'] for source in sources for rule in source.rules]
    hints_ok = accepts_hints(generator)
    accepted = {category: [] for category in targets}
    seen = set()
    drops, last_error = Counter(), None

    def call(category, size, focus, avoid, ask):
        kwargs = {'policies': policies, 'count': ask, 'categories': [category]}
        if hints_ok:
            kwargs['hints'] = {'focusRuleKeys': focus, 'avoidQuestions': avoid}
        try:
            return generator.generate(**kwargs)
        except Exception as exc:  # one failed chunk must not discard the other chunks
            return exc

    for round_index, chunk in enumerate(GENERATION_ROUNDS):
        jobs = generation_jobs(targets, accepted, chunk, rule_keys, round_index)
        if not jobs:
            break
        # Show the model everything accepted so far so refills do not re-propose earlier questions.
        avoid = [text for questions in accepted.values() for text, _ in questions]
        with ThreadPoolExecutor(max_workers=min(GENERATION_WORKERS, len(jobs))) as pool:
            # Refill rounds ask for one spare draft; extras are trimmed, so a dropped draft is still covered.
            spare = 1 if round_index else 0
            results = list(pool.map(lambda job: call(*job, avoid, job[1] + spare), jobs))
        for (category, size, _), result in zip(jobs, results):
            if isinstance(result, Exception):
                # Incomplete/timeout/throttled chunks are refilled by the next, smaller round.
                if isinstance(result, ApiError) and result.code in FATAL_GENERATION_ERRORS:
                    raise result
                last_error = result
                continue
            try:
                questions, dropped = parse_generated_questions(result)
            except (ValueError, TypeError, AttributeError):
                drops['schema'] += 1
                continue
            drops['schema'] += dropped
            taken = 0
            for question in questions:
                key = normalize_question(question.question)
                if question.category != category:
                    drops['category'] += 1
                    continue
                if key in seen or not question.expectedConditions or taken >= size \
                        or len(accepted[category]) >= targets[category]:
                    continue
                try:
                    hydrated = service.hydrate_generated_question(sources, question)
                    payload = service.question_payload(db, project_id, version, hydrated, generated=True)
                except ApiError as exc:
                    drops['conditions' if exc.code in ('INVALID_CONDITION_ID', 'INVALID_EXPECTED_CONDITIONS')
                          else 'evidence'] += 1
                    continue
                except (ValidationError, ValueError):
                    drops['schema'] += 1
                    continue
                seen.add(key)
                taken += 1
                accepted[category].append((question.question, payload))
    payloads = [payload for questions in accepted.values() for _, payload in questions]
    if payloads:
        # A shortfall after every refill round still keeps the usable drafts for review.
        return payloads
    if drops:
        code, message = DROP_ERRORS[drops.most_common(1)[0][0]]
        raise ApiError(502, code, message)
    if last_error is not None:
        raise last_error
    raise ApiError(502, 'GENERATION_SCHEMA_INVALID', '생성 결과의 형식을 확인할 수 없습니다.')


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
    rows, next_cursor = service.paginate(db, select(TestSet).where(TestSet.project_id == project.id, TestSet.deleted.is_(False)),
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
def new_draft(testSetId: ResourceId, body: NewDraft, project: OwnedProject, user: CurrentUser, db: Db, response: Response):
    """Copy a confirmed version to a new draft and require fresh human review."""
    row = service.testset_row(db, project.id, testSetId)
    source = service.version_row(db, testSetId, body.sourceVersionId)
    if source.status != 'confirmed':
        raise ApiError(409, 'TESTSET_NOT_CONFIRMED', '확정된 버전에서 새 초안을 만드세요.')
    if body.policyVersionIds is not None:
        service.policy_versions(db, project.id, body.policyVersionIds)
    result = db.execute(update(TestSet).where(TestSet.id == row.id, TestSet.revision == body.expectedRevision, TestSet.deleted.is_(False))
        .values(revision=TestSet.revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 테스트셋 revision으로 다시 요청해 주세요.')
    db.refresh(row)
    number = db.scalar(select(func.max(TestSetVersion.version)).where(TestSetVersion.testset_id == row.id)) + 1
    version = TestSetVersion(id=service.uid(), testset_id=row.id, version=number, revision=1, status='draft',
        policy_version_ids=body.policyVersionIds if body.policyVersionIds is not None else list(source.policy_version_ids), snapshot=None, created_at=now())
    db.add(version)
    db.flush()
    for question in service.questions(db, source.id) if body.policyVersionIds is None else []:
        payload = {**question.payload, 'expectedConditions': service.question_data(question)['expectedConditions']}
        copied = TestQuestion(id=service.uid(), testset_version_id=version.id, payload=payload,
            review_status='pending', created_at=now())
        db.add(copied)
        service.record_history(db, version, copied, user.id, 'copied', note=f'원본 문항: {question.id}')
    db.commit()
    response.headers['Location'] = f'/api/projects/{project.id}/testsets/{row.id}/versions/{version.id}'
    return {'data': service.version_data(db, version)}


@router.post('/{testSetId}/versions/{versionId}/questions', status_code=201, response_model=Data[TestSetVersionOut])
def add_question(testSetId: ResourceId, versionId: ResourceId, body: QuestionWrite, project: OwnedProject, user: CurrentUser, db: Db, response: Response):
    """Create a source-grounded manual question with an auditable initial state."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    ensure_capacity(db, version.id, 1)
    row = TestQuestion(id=service.uid(), testset_version_id=version.id,
        payload=service.question_payload(db, project.id, version, body), review_status='pending', created_at=now())
    db.add(row)
    service.record_history(db, version, row, user.id, 'created', note=body.changeNote)
    db.commit()
    response.headers['Location'] = f'/api/projects/{project.id}/testsets/{testSetId}/versions/{version.id}/questions/{row.id}'
    return {'data': service.version_data(db, version)}


@router.post('/{testSetId}/versions/{versionId}/questions/import', status_code=201, response_model=Data[TestSetVersionOut])
def import_questions(testSetId: ResourceId, versionId: ResourceId, project: OwnedProject, user: CurrentUser,
                     db: Db, request: Request,
                     file: Annotated[UploadFile, File(description='UTF-8 CSV, 최대 2MiB·200행. question,category,modelAnswer 헤더')],
                     expectedRevision: Annotated[int, Form(ge=1, le=2**31 - 1)]):
    """Validate every row before atomically appending pending, unlinked questions."""
    version = resolve(db, project.id, testSetId, versionId)
    try:
        if not (file.filename or '').lower().endswith('.csv'):
            raise ApiError(415, 'CSV_FILE_REQUIRED', '.csv 파일을 업로드하세요.')
        bodies = parse_questions(file.file.read(MAX_CSV_BYTES + 1))
    except CsvValidationError as exc:
        return error_response(request.scope, exc.status, exc.code, exc.message, field_errors=exc.field_errors)
    finally:
        file.file.close()
    service.claim_draft(db, version, expectedRevision)
    ensure_capacity(db, version.id, len(bodies))
    for body in bodies:
        row = TestQuestion(id=service.uid(), testset_version_id=version.id,
            payload=service.question_payload(db, project.id, version, body), review_status='pending', created_at=now())
        db.add(row)
        service.record_history(db, version, row, user.id, 'created', note='CSV 가져오기')
    db.commit()
    return {'data': service.version_data(db, version)}


@router.get('/{testSetId}/versions/{versionId}/questions/{questionId}', response_model=Data[QuestionOut])
def get_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, project: OwnedProject, db: Db):
    """Return a single question after checking every parent scope."""
    version = resolve(db, project.id, testSetId, versionId)
    return {'data': service.question_data(question_row(db, version.id, questionId))}


@router.patch('/{testSetId}/versions/{versionId}/questions/{questionId}', response_model=Data[TestSetVersionOut])
def edit_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, body: QuestionWrite, project: OwnedProject, user: CurrentUser, db: Db):
    """Replace a draft question and invalidate any prior human approval."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    row = question_row(db, version.id, questionId)
    before = service.question_data(row)
    row.payload = service.question_payload(db, project.id, version, body, previous=before)
    row.review_status, row.reviewed_by, row.review_note = 'pending', None, None
    row.reviewed_at = None
    service.record_history(db, version, row, user.id, 'edited', before=before, note=body.changeNote)
    db.commit()
    return {'data': service.version_data(db, version)}


@router.post('/{testSetId}/versions/{versionId}/questions/{questionId}/review', response_model=Data[TestSetVersionOut])
def review_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, body: Review,
                    project: OwnedProject, user: CurrentUser, db: Db):
    """Record explicit human approval or rejection; generation never grants approval."""
    version = resolve(db, project.id, testSetId, versionId)
    service.claim_draft(db, version, body.expectedRevision)
    row = question_row(db, version.id, questionId)
    before = service.question_data(row)
    if body.status == 'approved':
        service.require_reviewable(db, project.id, version, row)
    row.review_status, row.reviewed_by, row.review_note = body.status, user.id, body.note
    # Match MySQL DATETIME precision before freezing the audit JSON pre-commit.
    row.reviewed_at = now().replace(microsecond=0)
    service.record_history(db, version, row, user.id, 'reviewed', before=before, note=body.note)
    db.commit()
    return {'data': service.version_data(db, version)}


@router.delete('/{testSetId}/versions/{versionId}/questions/{questionId}', status_code=204)
def delete_question(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, project: OwnedProject, user: CurrentUser, db: Db,
                    if_match: IfMatch = None):
    """Remove a draft-only question using the current version revision as If-Match."""
    version = resolve(db, project.id, testSetId, versionId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{version.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인해 주세요.')
    service.claim_draft(db, version, version.revision)
    row = question_row(db, version.id, questionId)
    service.record_history(db, version, row, user.id, 'deleted', before=service.question_data(row))
    db.delete(row)
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
    for row in rows:
        service.require_reviewable(db, project.id, version, row)
    sources = service.policy_versions(db, project.id, version.policy_version_ids, allow_deleted=True)
    version.status, version.confirmed_by, version.confirmed_at = 'confirmed', user.id, now()
    version.snapshot = {'schemaVersion': 2, 'projectId': project.id, 'testSetId': testSetId,
        'testSetVersionId': version.id, 'version': version.version, 'confirmedBy': user.id,
        'confirmedAt': service.iso(version.confirmed_at),
        'policies': service.freeze_policy_titles(db, project.id, [service.policy_version_data(p) for p in sources]),
        'questions': [service.question_data(q) for q in rows]}
    db.commit()
    return {'data': service.version_data(db, version)}


@router.get('/{testSetId}/versions/{versionId}/evaluation-snapshot', response_model=Data[EvaluationSnapshot], response_model_exclude_unset=True)
def evaluation_snapshot(testSetId: ResourceId, versionId: ResourceId, project: OwnedProject, db: Db):
    """Supply the evaluation team with an immutable, fully reviewed input snapshot."""
    version = resolve(db, project.id, testSetId, versionId)
    return {'data': service.get_evaluation_snapshot(db, project.id, version.id)}


@router.get('/{testSetId}/versions/{versionId}/history', response_model=Data[Page[QuestionHistoryOut]])
def version_history(testSetId: ResourceId, versionId: ResourceId, project: OwnedProject, db: Db,
                    limit: Limit = 20, cursor: Cursor = None):
    """Include deleted questions in the version's append-only review/edit timeline."""
    version = resolve(db, project.id, testSetId, versionId)
    rows, next_cursor = service.paginate(db, select(QuestionHistory).where(QuestionHistory.testset_version_id == version.id),
        QuestionHistory, f'questionHistory:{version.id}', limit, cursor, versioned=True, sequence=QuestionHistory.version_revision)
    return {'data': {'items': [service.history_data(row) for row in rows], 'nextCursor': next_cursor}}


@router.get('/{testSetId}/versions/{versionId}/questions/{questionId}/history', response_model=Data[Page[QuestionHistoryOut]])
def question_history(testSetId: ResourceId, versionId: ResourceId, questionId: ResourceId, project: OwnedProject, db: Db,
                     limit: Limit = 20, cursor: Cursor = None):
    version = resolve(db, project.id, testSetId, versionId)
    statement = select(QuestionHistory).where(QuestionHistory.testset_version_id == version.id, QuestionHistory.question_id == questionId)
    if db.scalar(statement.limit(1)) is None:
        question_row(db, version.id, questionId)  # Existing legacy questions legitimately have no recorded history.
    rows, next_cursor = service.paginate(db, statement, QuestionHistory, f'questionHistory:{version.id}:{questionId}',
        limit, cursor, versioned=True, sequence=QuestionHistory.version_revision)
    return {'data': {'items': [service.history_data(row) for row in rows], 'nextCursor': next_cursor}}


@router.post('/{testSetId}/versions/{versionId}/questions/generate', response_model=Data[TestSetVersionOut])
def generate(testSetId: ResourceId, versionId: ResourceId, body: Generate, project: OwnedProject,
             user: CurrentUser, db: Db, generator: Annotated[QuestionGenerator, Depends(get_question_generator)],
             idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=100)]):
    """Validate an injected provider's batch and save pending questions exactly once per key."""
    if not idempotency_key.strip():
        raise ApiError(400, 'VALIDATION_ERROR', 'Idempotency-Key는 공백일 수 없습니다.')
    version = resolve(db, project.id, testSetId, versionId)
    # A separate committed quota precedes draft locks; failed provider calls also count.
    retry_after = check_rate_limit(db, user.id, 'question-generation', 10)
    if retry_after is not None:
        raise ApiError(429, 'RATE_LIMITED', '질문 생성 요청이 너무 많습니다.', headers={'Retry-After': str(retry_after)})
    # Serialize generation calls for this version before checking replay or revision.
    db.execute(select(TestSetVersion.id).where(TestSetVersion.id == version.id).with_for_update())
    key = hashlib.sha256(f'{user.id}|{version.id}|{idempotency_key}'.encode()).hexdigest()
    # Preserve fingerprints of in-flight 24-hour replays created before categoryCounts existed.
    fingerprint_body = body.model_dump(exclude={'categoryCounts'} if body.categoryCounts is None else set())
    fingerprint = hashlib.sha256(json.dumps(fingerprint_body, sort_keys=True).encode()).hexdigest()
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
        payloads = generate_payloads(db, project.id, version, sources, generator, requested_category_counts(body))
    except ApiError:
        db.rollback()
        raise
    except (ValidationError, ValueError):
        db.rollback()
        raise ApiError(502, 'GENERATION_SCHEMA_INVALID', '생성 결과의 형식을 확인할 수 없습니다.') from None
    except TimeoutError:
        db.rollback()
        raise ApiError(504, 'GENERATION_TIMEOUT', '질문 생성 시간이 초과되었습니다.') from None
    except Exception:
        db.rollback()
        raise ApiError(503, 'GENERATION_UNAVAILABLE', '질문 생성 서비스를 사용할 수 없습니다.') from None
    for payload in payloads:
        row = TestQuestion(id=service.uid(), testset_version_id=version.id, payload=payload, review_status='pending', created_at=now())
        db.add(row)
        service.record_history(db, version, row, user.id, 'generated')
    db.flush()
    result = {'data': service.version_data(db, version)}
    db.add(GenerationRequest(key=key, request_hash=fingerprint, response=result, expires_at=now() + timedelta(hours=24)))
    db.commit()
    return result


@router.patch('/{testSetId}', response_model=Data[TestSetOut])
def rename_testset(testSetId: ResourceId, body: Rename, project: OwnedProject, db: Db):
    row = service.testset_row(db, project.id, testSetId)
    changed = db.execute(update(TestSet).where(TestSet.id == row.id, TestSet.deleted.is_(False),
        TestSet.revision == body.expectedRevision).values(name=body.name, revision=TestSet.revision + 1))
    if changed.rowcount != 1:
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision을 확인하세요.')
    db.commit()
    db.refresh(row)
    return {'data': service.testset_data(row)}


@router.delete('/{testSetId}', status_code=204)
def delete_testset(testSetId: ResourceId, project: OwnedProject, db: Db, if_match: IfMatch = None):
    row = service.testset_row(db, project.id, testSetId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{row.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인하세요.')
    changed = db.execute(update(TestSet).where(TestSet.id == row.id, TestSet.deleted.is_(False),
        TestSet.revision == row.revision).values(deleted=True, revision=TestSet.revision + 1))
    if changed.rowcount != 1:
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인하세요.')
    db.commit()
    return Response(status_code=204)
