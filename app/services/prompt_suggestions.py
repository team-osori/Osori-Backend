"""Evidence adapter and lifecycle independent of evaluation/rerun/comparison storage."""
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.concurrency import is_write_conflict
from app.core.errors import ApiError
from app.models.evaluation import EvaluationItem
from app.models.prompt_suggestion import PromptSuggestion
from app.schemas.prompt_suggestion import GeneratedSuggestions
from app.services.auth import now
from app.services.evaluation_analysis import item_analysis
from app.services.evaluation_reviews import latest_reviews
from app.services.evaluations import get_run
from app.services.prompts import get_version

LIMITATIONS = ['NO_CALIBRATED_EFFECT_MODEL', 'TARGET_COUNT_IS_NOT_PREDICTED_IMPROVEMENT',
              'CAUSAL_ISOLATION_NOT_PERFORMED', 'PROMPT_APPLICATION_UNVERIFIED',
              'REGRESSION_POSSIBLE', 'MODEL_RATIONALE_REQUIRES_HUMAN_REVIEW']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def source_snapshot(db, service_id, body, *, lock=False):
    run = get_run(db, service_id, body.evaluationId)
    if (run.status not in ('COMPLETED', 'PARTIAL_FAILED', 'FAILED')
            or run.inputs.get('mode') != 'SINGLE' or run.inputs.get('criteria', {}).get('version') != 2):
        raise ApiError(409, 'INVALID_SUGGESTION_SOURCE', '종료된 SINGLE v2 평가를 선택하세요.')
    base = get_version(db, service_id, body.baseVersionId)
    prompt = run.inputs.get('prompts', {}).get('a')
    if not prompt or prompt['id'] != base.id or prompt['content'] != base.content:
        raise ApiError(409, 'SUGGESTION_PROMPT_MISMATCH', '평가에 사용한 프롬프트 원문 버전을 선택하세요.')
    statement = select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id,
        EvaluationItem.id.in_(body.itemIds)).order_by(EvaluationItem.id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    items = db.scalars(statement).all()
    if len(items) != len(body.itemIds):
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가 문항을 찾을 수 없습니다.')
    reviews = latest_reviews(db, body.itemIds)
    policies = {p['id']: p['content'] for p in run.inputs['testSet']['policies']}
    evidence = []
    for item in items:
        if not item.a or item.a['callStatus'] != 'SUCCEEDED':
            raise ApiError(409, 'NO_FAILED_CONDITION', '성공한 답변의 미충족 조건을 선택하세요.')
        detail = item_analysis(run, item, reviews)['a']
        failed = [c for c in detail['conditions'] if c['judgment']
                  and c['judgment']['status'] == 'judged' and c['judgment']['verdict'] == 'fail']
        if not failed:
            raise ApiError(409, 'NO_FAILED_CONDITION', '확인된 미충족 조건이 없습니다.')
        for condition in failed:
            source = condition['policyEvidence']
            if not source['quote'] or source['quote'] not in policies.get(source['policyVersionId'], ''):
                raise ApiError(409, 'INVALID_SUGGESTION_EVIDENCE', '정책 원문 근거를 확인할 수 없습니다.')
            issue = next((i for i in detail['issues'] if i['conditionId'] == condition['conditionId']), None)
            evidence.append({'id': f'e{len(evidence) + 1}', 'itemId': item.id, 'questionId': item.question_id,
                'question': item.input['question'], 'answer': item.a['answer'],
                'reviewRevision': item.review_revision, 'condition': condition, 'issue': issue,
                'facts': detail['facts'], 'hypotheses': detail['hypotheses'], 'limitations': detail['limitations']})
    return {'promptId': base.prompt_id, 'content': base.content, 'itemIds': body.itemIds,
        'sourceHash': digest({'inputs': run.inputs, 'items': [
            [item.id, item.input, item.a, item.review_revision] for item in items]}),
        'analysisBasis': 'LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC', 'evidence': evidence}


def validate_generated(output, snapshot):
    parsed = GeneratedSuggestions.model_validate(output)
    content = snapshot['content']
    allowed = {e['id'] for e in snapshot['evidence']}
    result = []
    for edit in parsed.suggestions:
        start = content.find(edit.originalText)
        if (start < 0 or content.find(edit.originalText, start + 1) >= 0 or edit.originalText == edit.replacementText
                or not edit.reason.strip() or len(set(edit.evidenceIds)) != len(edit.evidenceIds)
                or not set(edit.evidenceIds) <= allowed):
            raise ValueError('Invalid source reference')
        candidate = content[:start] + edit.replacementText + content[start + len(edit.originalText):]
        if not candidate.strip() or len(candidate) > 100_000:
            raise ValueError('Invalid resulting prompt')
        result.append({**edit.model_dump(), 'id': str(uuid4()), 'start': start, 'end': start + len(edit.originalText)})
    return result


def preview(row, selected=None):
    selected = row.selected_ids if selected is None else selected
    edits = [s for s in row.suggestions if s['id'] in selected]
    if len(edits) != len(selected):
        raise ApiError(400, 'INVALID_SUGGESTION_SELECTION', '중복 없이 해당 수정안 ID를 선택하세요.')
    ordered = sorted(edits, key=lambda e: e['start'])
    if any(left['end'] > right['start'] for left, right in zip(ordered, ordered[1:])):
        raise ApiError(409, 'OVERLAPPING_SUGGESTIONS', '같은 원문 영역의 대안 중 하나만 선택하세요.')
    content = row.snapshot['content']
    for edit in reversed(ordered):
        content = content[:edit['start']] + edit['replacementText'] + content[edit['end']:]
    if not content.strip() or len(content) > 100_000:
        raise ApiError(400, 'INVALID_SUGGESTION_SELECTION', '적용 후 원문은 1~100000자여야 합니다.')
    return content


def estimate(row):
    evidence_ids = {eid for s in row.suggestions if s['id'] in row.selected_ids for eid in s['evidenceIds']}
    return {'expectedImprovedQuestionCount': None, 'method': 'NOT_ESTIMABLE', 'uncertainty': 'UNKNOWN',
        'targetedQuestionCount': len({e['questionId'] for e in row.snapshot['evidence'] if e['id'] in evidence_ids}),
        'limitations': LIMITATIONS}


def get_suggestion(db, service_id, suggestion_id):
    row = db.scalar(select(PromptSuggestion).where(PromptSuggestion.id == suggestion_id,
        PromptSuggestion.service_id == service_id))
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '수정 제안을 찾을 수 없습니다.')
    if row.status == 'GENERATING' and row.created_at.replace(tzinfo=None) < (now() - timedelta(minutes=5)).replace(tzinfo=None):
        # An interrupted request is never automatically sent to a paid provider again.
        db.execute(update(PromptSuggestion).where(PromptSuggestion.id == row.id,
            PromptSuggestion.status == 'GENERATING').values(status='FAILED', updated_at=now(),
            error={'code': 'SUGGESTION_INTERRUPTED', 'message': '생성 결과를 확인할 수 없습니다. 새 키로 다시 요청하세요.'}))
        db.commit()
        db.refresh(row)
    return row


def generate_suggestions(db, service_id, user_id, body, key, generator):
    key_hash, request_hash = digest([user_id, key]), digest(body.model_dump())
    existing = db.scalar(select(PromptSuggestion).where(PromptSuggestion.service_id == service_id,
        PromptSuggestion.idempotency_hash == key_hash))
    if existing:
        if existing.request_hash != request_hash:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 키에 다른 요청을 사용할 수 없습니다.')
        return get_suggestion(db, service_id, existing.id), False
    snapshot = source_snapshot(db, service_id, body)
    snapshot['generator'] = generator.descriptor()
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 500 * 1024:
        raise ApiError(400, 'SUGGESTION_INPUT_TOO_LARGE', '선택한 문항이나 프롬프트의 범위를 줄여 주세요.')
    row = PromptSuggestion(id=str(uuid4()), service_id=service_id, evaluation_id=body.evaluationId,
        base_version_id=body.baseVersionId, idempotency_hash=key_hash, request_hash=request_hash,
        status='GENERATING', revision=1, snapshot=snapshot, suggestions=[], selected_ids=[],
        created_at=now(), updated_at=now())
    db.add(row)
    try:
        db.commit()  # Claim before any paid call; release the DB transaction during generation.
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(PromptSuggestion).where(PromptSuggestion.service_id == service_id,
            PromptSuggestion.idempotency_hash == key_hash))
        if existing is None:
            raise
        if existing.request_hash != request_hash:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '같은 키에 다른 요청을 사용할 수 없습니다.') from None
        return get_suggestion(db, service_id, existing.id), False
    try:
        edits = validate_generated(generator.generate({'content': snapshot['content'], 'evidence': snapshot['evidence']}), snapshot)
        changes = {'status': 'READY', 'suggestions': edits, 'error': None}
    except ApiError as exc:
        changes = {'status': 'FAILED', 'error': {'code': exc.code, 'message': exc.message}}
    except Exception:
        # Invalid structured output / unexpected adapter errors must never leak raw provider content.
        changes = {'status': 'FAILED', 'error': {'code': 'INVALID_SUGGESTION_OUTPUT', 'message': '모델의 수정안과 원문 근거를 검증하지 못했습니다.'}}
    db.execute(update(PromptSuggestion).where(PromptSuggestion.id == row.id,
        PromptSuggestion.status == 'GENERATING').values(**changes, updated_at=now()))
    db.commit()
    db.refresh(row)
    return row, True


def guarded_update(db, row, revision, **values):
    try:
        changed = db.execute(update(PromptSuggestion).where(PromptSuggestion.id == row.id,
            PromptSuggestion.status == 'READY', PromptSuggestion.revision == revision).values(
            **values, revision=PromptSuggestion.revision + 1, updated_at=now())
            .execution_options(synchronize_session=False))
    except OperationalError as exc:
        db.rollback()
        if is_write_conflict(exc):
            raise ApiError(409, 'SUGGESTION_CONFLICT', '최신 수정 제안 상태를 확인하세요.') from None
        raise
    if changed.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'SUGGESTION_CONFLICT', '최신 수정 제안 상태와 revision을 확인하세요.')


def apply_suggestions(db, service_id, prompt, body):
    from app.schemas.prompt_suggestion import SuggestionCreate
    from app.services.prompts import save_version

    # A current locking read makes simultaneous retries replay the committed version on MySQL.
    row = db.scalar(select(PromptSuggestion).where(PromptSuggestion.id == body.suggestionId,
        PromptSuggestion.service_id == service_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '수정 제안을 찾을 수 없습니다.')
    if row.snapshot['promptId'] != prompt.id or row.base_version_id != body.baseVersionId:
        raise ApiError(409, 'SUGGESTION_PROMPT_MISMATCH', '수정 제안의 원본 프롬프트를 확인하세요.')
    application_hash = digest(body.model_dump())
    if row.status == 'APPLIED':
        if row.application_hash != application_hash:
            raise ApiError(409, 'SUGGESTION_ALREADY_APPLIED', '이미 적용한 수정 제안입니다.')
        # The winning insert may be newer than this transaction's repeatable-read snapshot.
        return get_version(db, service_id, row.applied_version_id, lock=True)
    if row.status != 'READY' or row.revision != body.suggestionRevision:
        raise ApiError(409, 'SUGGESTION_CONFLICT', '최신 수정 제안 상태와 revision을 확인하세요.')
    if not row.selected_ids:
        raise ApiError(409, 'SUGGESTION_SELECTION_REQUIRED', '적용할 수정안을 선택하세요.')
    if body.content != preview(row):
        raise ApiError(409, 'SUGGESTION_CONTENT_MISMATCH', '선택한 수정안의 previewContent를 그대로 저장하세요.')
    current = source_snapshot(db, service_id, SuggestionCreate(evaluationId=row.evaluation_id,
        baseVersionId=row.base_version_id, itemIds=row.snapshot['itemIds']), lock=True)
    if current['sourceHash'] != row.snapshot['sourceHash']:
        raise ApiError(409, 'SUGGESTION_EVIDENCE_CHANGED', '원본 분석 또는 사람 검토가 바뀌었습니다. 새 제안을 생성하세요.')
    guarded_update(db, row, body.suggestionRevision, status='APPLIED', application_hash=application_hash)
    version = save_version(db, service_id, prompt, body)
    db.execute(update(PromptSuggestion).where(PromptSuggestion.id == row.id).values(applied_version_id=version.id))
    return version
