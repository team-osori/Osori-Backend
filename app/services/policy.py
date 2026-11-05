import base64
import json
import re
from copy import deepcopy
from datetime import UTC
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, or_, select, update

from app.core.errors import ApiError
from app.core.cursors import cursor_id, cursor_integer, cursor_time, decode_cursor
from app.models.policy import Policy, PolicyVersion, QuestionHistory, TestQuestion, TestSet, TestSetVersion
from app.schemas.policy import GeneratedQuestion, QuestionBody
from app.services.auth import now


def uid():
    """Allocate an opaque identifier for a policy, test set, version or question."""
    return str(uuid4())


def iso(value):
    """Normalize MySQL naive UTC timestamps for API responses and snapshots."""
    return value.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z') if value else None


def not_found():
    """Hide missing, cross-project and logically deleted resources consistently."""
    return ApiError(404, 'RESOURCE_NOT_FOUND', '대상을 찾을 수 없습니다.')


def policy_row(db, project_id, policy_id):
    """Resolve an active policy only within the authorized project."""
    row = db.scalar(select(Policy).where(Policy.id == policy_id, Policy.project_id == project_id, Policy.deleted.is_(False)))
    if row is None:
        raise not_found()
    return row


def testset_row(db, project_id, testset_id):
    """Resolve a test set without exposing a different project's resources."""
    row = db.scalar(select(TestSet).where(TestSet.id == testset_id, TestSet.project_id == project_id, TestSet.deleted.is_(False)))
    if row is None:
        raise not_found()
    return row


def version_row(db, testset_id, version_id):
    """Resolve a version only within its parent test set."""
    row = db.scalar(select(TestSetVersion).where(TestSetVersion.id == version_id, TestSetVersion.testset_id == testset_id))
    if row is None:
        raise not_found()
    return row


def policy_versions(db, project_id, ids, *, allow_deleted=False):
    """Validate all policy references before storing or exposing a test-set snapshot."""
    statement = select(PolicyVersion).join(Policy).where(Policy.project_id == project_id, PolicyVersion.id.in_(ids))
    if not allow_deleted:
        statement = statement.where(Policy.deleted.is_(False))
    rows = db.scalars(statement).all()
    if len(rows) != len(set(ids)):
        raise not_found()
    by_id = {row.id: row for row in rows}
    return [by_id[id_] for id_ in ids]


def claim_draft(db, version, revision):
    """Lock a draft via a conditional update; confirmed versions never accept edits."""
    result = db.execute(update(TestSetVersion).where(TestSetVersion.id == version.id,
        TestSetVersion.revision == revision, TestSetVersion.status == 'draft')
        .values(revision=TestSetVersion.revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        db.refresh(version)
        if version.status == 'confirmed':
            raise ApiError(409, 'TESTSET_IMMUTABLE', '확정된 버전은 수정할 수 없습니다. 새 버전을 만드세요.')
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision으로 다시 요청해 주세요.')
    db.refresh(version)


def legacy_conditions(payload, question_id):
    """Derive stable IDs for old draft data without rewriting frozen historical snapshots."""
    if payload.get('policyVersionId') is None:
        return None
    source = {key: payload[key] for key in ('policyVersionId', 'ruleKeys', 'evidenceQuote')}
    behaviors = [('required', payload['expectedBehavior'])] + [('forbidden', text) for text in payload['forbiddenBehaviors']]
    return [{'id': str(uuid5(NAMESPACE_URL, f'osori:{question_id}:{index}')), 'description': text,
             'kind': kind, 'category': 'policy', **source} for index, (kind, text) in enumerate(behaviors)]


def source_quote(quote, content, *, normalize_whitespace=False):
    """Restore the exact source span, allowing only whitespace differences in model output."""
    if quote in content or not normalize_whitespace:
        return quote
    # Preserve word boundaries: absent whitespace is not equivalent to a space.
    words = re.split(r'\s+', quote.strip())
    if not words or not words[0]:
        return quote
    match = re.search(r'\s+'.join(re.escape(word) for word in words), content)
    if match and len(match.group()) <= 10000:
        return match.group()
    return quote


MAX_GENERATED_FORBIDDEN = 20
MAX_GENERATED_REQUIRED_CHARS = 2000


def hydrate_generated_question(sources, question: GeneratedQuestion) -> QuestionBody:
    """Attach immutable policy identities and verbatim evidence to a semantic model draft.

    Model slips are repaired instead of failing the batch: unknown rule keys are dropped,
    a condition with no real rule is dropped, and limits are trimmed. The question is
    rejected only when no required condition is left that cites a real rule.
    """
    rule_maps = [(source, {rule['key']: rule for rule in source.rules}) for source in sources]

    def pick(rule_keys, prefer=None):
        keys = list(dict.fromkeys(rule_keys))
        best = None
        for source, rules in rule_maps:
            kept = [key for key in keys if key in rules]
            rank = (len(kept), source is prefer)
            if kept and (best is None or rank > best[0]):
                best = (rank, source, rules, kept)
        return None if best is None else best[1:]

    primary = pick(question.ruleKeys)
    conditions, required_chars = [], 0
    for condition in question.expectedConditions:
        found = pick(condition.ruleKeys, prefer=primary[0] if primary else None)
        if found is None:
            continue
        source, rules, kept = found
        if condition.kind == 'forbidden':
            if sum(c['kind'] == 'forbidden' for c in conditions) >= MAX_GENERATED_FORBIDDEN:
                continue
        else:
            added = len(condition.description) + (1 if required_chars else 0)
            if required_chars + added > MAX_GENERATED_REQUIRED_CHARS:
                continue
            required_chars += added
        conditions.append({
            'description': condition.description,
            'kind': condition.kind,
            'category': condition.category,
            'policyVersionId': source.id,
            'ruleKeys': kept,
            'evidenceQuote': rules[kept[0]]['sourceQuote'],
        })
    if not any(condition['kind'] == 'required' for condition in conditions):
        raise ApiError(400, 'INVALID_POLICY_EVIDENCE', '생성 문항의 규칙 key를 정책에서 확인할 수 없습니다.')
    if primary is None:
        # The question-level keys were all invented; fall back to the first grounded condition.
        first = next(c for c in conditions if c['kind'] == 'required')
        primary = pick(first['ruleKeys'])
    source, rules, kept = primary
    return QuestionBody(
        question=question.question,
        expectedBehavior=question.expectedBehavior or question.modelAnswer,
        modelAnswer=question.modelAnswer,
        forbiddenBehaviors=question.forbiddenBehaviors[:MAX_GENERATED_FORBIDDEN],
        policyVersionId=source.id,
        ruleKeys=kept,
        evidenceQuote=rules[kept[0]]['sourceQuote'],
        category=question.category,
        expectedConditions=conditions,
    )


def repair_generated_evidence(db, project_id, version, body: QuestionBody) -> QuestionBody:
    """Fix two observed model slips in generated drafts before the usual strict validation.

    1. The model copies `policyId` into `policyVersionId` (they sit side by side in the payload).
       Map it back only when it names exactly one policy included in this test-set version.
    2. The model joins real sentences from different places into one quote. When every sentence of
       the quote exists in the source, keep the first one as the (contiguous) quote. A quote with any
       sentence that is not in the source is left unchanged so fabricated evidence is still rejected.
    3. The model adds a rule key that does not exist next to real ones. Drop only the unknown keys;
       if none of the cited keys exist, leave them so the question is still rejected.
    Human-written questions never pass through here.
    """
    sources = policy_versions(db, project_id, version.policy_version_ids, allow_deleted=True)
    by_policy = {}
    for source in sources:
        by_policy.setdefault(source.policy_id, []).append(source)
    by_version = {source.id: source for source in sources}

    def resolve(policy_version_id):
        if policy_version_id in by_version:
            return policy_version_id
        matches = by_policy.get(policy_version_id) or []
        return matches[0].id if len(matches) == 1 else policy_version_id

    def repair_quote(policy_version_id, quote):
        source = by_version.get(policy_version_id)
        if source is None or quote is None:
            return quote
        if source_quote(quote, source.content, normalize_whitespace=True) in source.content:
            return quote
        sentences = [part.strip() for part in re.split(r'(?<=[.!?。])\s+', quote) if part.strip()]
        spans = [source_quote(part, source.content, normalize_whitespace=True) for part in sentences]
        if len(sentences) > 1 and all(span in source.content for span in spans):
            return spans[0]
        return quote

    def known_keys(policy_version_id, keys):
        source = by_version.get(policy_version_id)
        if source is None:
            return keys
        valid = {rule['key'] for rule in source.rules}
        kept = [key for key in keys if key in valid]
        return kept or keys

    body = body.model_copy(deep=True)
    if body.policyVersionId is not None:
        body.policyVersionId = resolve(body.policyVersionId)
        body.evidenceQuote = repair_quote(body.policyVersionId, body.evidenceQuote)
        body.ruleKeys = known_keys(body.policyVersionId, body.ruleKeys)
    for condition in body.expectedConditions or []:
        condition.policyVersionId = resolve(condition.policyVersionId)
        condition.evidenceQuote = repair_quote(condition.policyVersionId, condition.evidenceQuote)
        condition.ruleKeys = known_keys(condition.policyVersionId, condition.ruleKeys)
    return body


def question_payload(db, project_id, version, body: QuestionBody, *, previous=None, generated=False):
    """Allow unlinked drafts; validate all supplied evidence and preserve condition IDs."""
    if generated:
        body = repair_generated_evidence(db, project_id, version, body)
    if body.policyVersionId is None:
        if generated:
            raise ApiError(400, 'INVALID_POLICY_EVIDENCE', '생성 문항에는 실제 정책 근거가 필요합니다.')
        return body.model_dump(exclude={'expectedRevision', 'changeNote'})
    if body.policyVersionId not in version.policy_version_ids:
        raise ApiError(400, 'INVALID_POLICY_REFERENCE', '테스트셋에 포함된 정책 버전을 선택하세요.')
    policy = policy_versions(db, project_id, [body.policyVersionId], allow_deleted=True)[0]
    body = body.model_copy(deep=True)
    body.evidenceQuote = source_quote(body.evidenceQuote, policy.content, normalize_whitespace=generated)
    keys = {rule['key'] for rule in policy.rules}
    if len(set(body.ruleKeys)) != len(body.ruleKeys) or not set(body.ruleKeys) <= keys or body.evidenceQuote not in policy.content:
        raise ApiError(400, 'INVALID_POLICY_EVIDENCE', '규칙 또는 근거 원문을 확인해 주세요.')
    payload = body.model_dump(exclude={'expectedRevision', 'changeNote'})
    if payload['expectedConditions'] is None:
        # Compatibility for old clients. Preserve IDs only for unchanged conditions.
        conditions = legacy_conditions(payload, uid())
        available = (previous or {}).get('expectedConditions') or []
        for condition in conditions:
            match = next((c for c in available if all(c.get(k) == v for k, v in condition.items() if k != 'id')), None)
            if match:
                condition['id'] = match['id']
                # Consume each ID once without mutating the audit's original list.
                available = [c for c in available if c['id'] != match['id']]
    else:
        conditions = payload['expectedConditions']
        allowed_ids = {c['id'] for c in ((previous or {}).get('expectedConditions') or [])}
        supplied = [c['id'] for c in conditions if c['id'] is not None]
        if len(set(supplied)) != len(supplied) or not set(supplied) <= allowed_ids:
            raise ApiError(400, 'INVALID_CONDITION_ID', '기존 조건 ID를 유지하거나 새 조건의 ID를 생략하세요.')
        for condition in conditions:
            condition['id'] = condition['id'] or uid()
        required = [c['description'] for c in conditions if c['kind'] == 'required']
        forbidden = [c['description'] for c in conditions if c['kind'] == 'forbidden']
        if not required or len('\n'.join(required)) > 2000 or len(forbidden) > 20:
            raise ApiError(400, 'INVALID_EXPECTED_CONDITIONS', '필수 조건과 기대 행동의 길이를 확인하세요.')
        # Old B-side consumers still receive summaries consistent with the new conditions.
        payload['expectedBehavior'] = '\n'.join(required)
        payload['forbiddenBehaviors'] = forbidden
    sources = {p.id: p for p in policy_versions(db, project_id, version.policy_version_ids, allow_deleted=True)}
    for condition in conditions:
        source = sources.get(condition['policyVersionId'])
        if source is None:
            raise ApiError(400, 'INVALID_POLICY_REFERENCE', '조건의 정책 버전을 확인하세요.')
        rule_keys = condition['ruleKeys']
        condition['evidenceQuote'] = source_quote(condition['evidenceQuote'], source.content,
                                                  normalize_whitespace=generated)
        if (len(rule_keys) != len(set(rule_keys)) or not set(rule_keys) <= {r['key'] for r in source.rules}
                or condition['evidenceQuote'] not in source.content):
            raise ApiError(400, 'INVALID_POLICY_EVIDENCE', '조건의 규칙 또는 근거 원문을 확인하세요.')
    payload['expectedConditions'] = conditions
    return payload



def require_reviewable(db, project_id, version, row):
    """Keep the v2 evaluator's complete-evidence invariant at both review and freeze."""
    if row.payload.get('policyVersionId') is None:
        raise ApiError(409, 'QUESTION_EVIDENCE_REQUIRED', '문항 근거를 연결한 뒤 승인·확정하세요.')
    data = question_data(row)
    body = QuestionBody.model_validate({key: data[key] for key in QuestionBody.model_fields if key in data})
    question_payload(db, project_id, version, body, previous=data)


def policy_data(row):
    """Serialize the stable policy identity separately from immutable content versions."""
    return {'id': row.id, 'projectId': row.project_id, 'name': row.name, 'revision': row.revision,
            'createdAt': iso(row.created_at)}


def policy_version_data(row):
    """Return original policy content and its immutable compliance-rule definitions."""
    return {'id': row.id, 'policyId': row.policy_id, 'version': row.version, 'content': row.content,
            'rules': row.rules, 'sourceDocument': row.source_document, 'createdAt': iso(row.created_at)}


def freeze_policy_titles(db, project_id, policies):
    """Copy display names into a new snapshot; never look them up at detail-read time."""
    result = deepcopy(policies)
    missing = {policy['policyId'] for policy in result if 'policyTitle' not in policy and policy.get('policyId')}
    titles = dict(db.execute(select(Policy.id, Policy.name).where(
        Policy.project_id == project_id, Policy.id.in_(missing))).all()) if missing else {}
    for policy in result:
        policy.setdefault('policyTitle', titles.get(policy.get('policyId')))
    return result


def testset_data(row):
    """Return the test-set identity and revision used when allocating a new draft."""
    return {'id': row.id, 'projectId': row.project_id, 'name': row.name, 'revision': row.revision,
            'createdAt': iso(row.created_at)}


def question_data(row):
    """Expose human review state along with original question and evidence fields."""
    return {'id': row.id, 'modelAnswer': None, **row.payload,
            'expectedConditions': row.payload.get('expectedConditions') or legacy_conditions(row.payload, row.id),
            'reviewStatus': row.review_status, 'reviewedBy': row.reviewed_by,
            'reviewNote': row.review_note, 'reviewedAt': iso(row.reviewed_at)}


def record_history(db, version, row, actor_id, action, *, before=None, note=None):
    """Write the full before/after state in the same transaction as the user action."""
    db.add(QuestionHistory(id=uid(), testset_version_id=version.id, question_id=row.id,
        version_revision=version.revision, action=action, actor_id=actor_id, note=note,
        before=deepcopy(before), after=None if action == 'deleted' else deepcopy(question_data(row)), created_at=now()))


def history_data(row):
    return {'id': row.id, 'questionId': row.question_id, 'versionRevision': row.version_revision,
            'action': row.action, 'actorId': row.actor_id, 'note': row.note,
            'before': row.before, 'after': row.after, 'createdAt': iso(row.created_at)}


def questions(db, version_id, *, lock=False):
    """Return a stable ordering of all questions in one bounded test-set version."""
    statement = select(TestQuestion).where(TestQuestion.testset_version_id == version_id).order_by(TestQuestion.created_at, TestQuestion.id)
    return db.scalars(statement.with_for_update() if lock else statement).all()


def version_summary(row):
    """List version metadata without loading every historical question payload."""
    return {'id': row.id, 'testSetId': row.testset_id, 'version': row.version, 'revision': row.revision,
            'status': row.status, 'policyVersionIds': row.policy_version_ids,
            'confirmedBy': row.confirmed_by, 'confirmedAt': iso(row.confirmed_at), 'createdAt': iso(row.created_at)}


def paginate(db, statement, model, scope, limit, cursor, *, versioned=False, sequence=None):
    """Use scoped keyset pagination, with version ordering for immutable version histories."""
    column = sequence if sequence is not None else (model.version if versioned else model.created_at)
    if cursor:
        try:
            data = decode_cursor(cursor, {'scope', 'v', 'id', 'value'})
            if data['scope'] != scope or type(data['v']) is not int or data['v'] != 1:
                raise ValueError()
            cursor_id(data['id'])
            value = cursor_integer(data['value']) if versioned else cursor_time(data['value'])
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
            raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None
        statement = statement.where(or_(column < value, and_(column == value, model.id < data['id'])))
    rows = db.scalars(statement.order_by(column.desc(), model.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        value = getattr(last, column.key) if versioned else last.created_at.isoformat()
        next_cursor = base64.urlsafe_b64encode(json.dumps({'scope': scope, 'v': 1, 'value': value, 'id': last.id}).encode()).decode()
    return rows[:limit], next_cursor


def version_data(db, row):
    """Serialize one bounded draft or confirmed review queue."""
    rows = questions(db, row.id)
    return {**version_summary(row), 'questions': [question_data(q) for q in rows],
            'questionCount': len(rows),
            'approvedCount': sum(q.review_status == 'approved' and q.reviewed_by is not None for q in rows)}


def get_evaluation_snapshot(db, project_id, version_id):
    """Return frozen inputs for an already authorized project; never accept a draft for evaluation."""
    row = db.scalar(select(TestSetVersion).join(TestSet).where(TestSet.project_id == project_id,
        TestSet.deleted.is_(False), TestSetVersion.id == version_id))
    if row is None:
        raise not_found()
    if row.status != 'confirmed' or row.snapshot is None:
        raise ApiError(409, 'TESTSET_NOT_CONFIRMED', '확정된 테스트셋 버전만 평가할 수 있습니다.')
    return row.snapshot
