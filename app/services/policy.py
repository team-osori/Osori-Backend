import base64
import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from app.core.errors import ApiError
from app.models.policy import Policy, PolicyVersion, TestQuestion, TestSet, TestSetVersion
from app.schemas.policy import QuestionBody
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
    row = db.scalar(select(TestSet).where(TestSet.id == testset_id, TestSet.project_id == project_id))
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


def question_payload(db, project_id, version, body: QuestionBody):
    """Require explicit source version, valid rules and a verbatim policy passage."""
    if body.policyVersionId not in version.policy_version_ids:
        raise ApiError(400, 'INVALID_POLICY_REFERENCE', '테스트셋에 포함된 정책 버전을 선택하세요.')
    policy = policy_versions(db, project_id, [body.policyVersionId], allow_deleted=True)[0]
    keys = {rule['key'] for rule in policy.rules}
    if len(set(body.ruleKeys)) != len(body.ruleKeys) or not set(body.ruleKeys) <= keys or body.evidenceQuote not in policy.content:
        raise ApiError(400, 'INVALID_POLICY_EVIDENCE', '규칙 또는 근거 원문을 확인해 주세요.')
    return body.model_dump(exclude={'expectedRevision'})


def policy_data(row):
    """Serialize the stable policy identity separately from immutable content versions."""
    return {'id': row.id, 'projectId': row.project_id, 'name': row.name, 'revision': row.revision,
            'createdAt': iso(row.created_at)}


def policy_version_data(row):
    """Return original policy content and its immutable compliance-rule definitions."""
    return {'id': row.id, 'policyId': row.policy_id, 'version': row.version, 'content': row.content,
            'rules': row.rules, 'createdAt': iso(row.created_at)}


def testset_data(row):
    """Return the test-set identity and revision used when allocating a new draft."""
    return {'id': row.id, 'projectId': row.project_id, 'name': row.name, 'revision': row.revision,
            'createdAt': iso(row.created_at)}


def question_data(row):
    """Expose human review state along with original question and evidence fields."""
    return {'id': row.id, **row.payload, 'reviewStatus': row.review_status,
            'reviewedBy': row.reviewed_by, 'reviewNote': row.review_note}


def questions(db, version_id, *, lock=False):
    """Return a stable ordering of all questions in one bounded test-set version."""
    statement = select(TestQuestion).where(TestQuestion.testset_version_id == version_id).order_by(TestQuestion.created_at, TestQuestion.id)
    return db.scalars(statement.with_for_update() if lock else statement).all()


def version_summary(row):
    """List version metadata without loading every historical question payload."""
    return {'id': row.id, 'testSetId': row.testset_id, 'version': row.version, 'revision': row.revision,
            'status': row.status, 'policyVersionIds': row.policy_version_ids,
            'confirmedBy': row.confirmed_by, 'confirmedAt': iso(row.confirmed_at), 'createdAt': iso(row.created_at)}


def cursor_object(pairs):
    """Reject duplicate JSON members instead of silently accepting the last value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate cursor member')
        result[key] = value
    return result


def invalid_json_constant(value):
    """Reject nonstandard JSON NaN and infinity tokens at the decoding boundary."""
    raise ValueError('Non-finite cursor constant')


def paginate(db, statement, model, scope, limit, cursor, *, versioned=False):
    """Use scoped keyset pagination, with version ordering for immutable version histories."""
    column = model.version if versioned else model.created_at
    if cursor:
        try:
            data = json.loads(base64.b64decode(cursor.encode(), altchars=b'-_', validate=True),
                              object_pairs_hook=cursor_object, parse_constant=invalid_json_constant)
            if (type(data) is not dict or set(data) != {'scope', 'v', 'id', 'value'}
                    or data['scope'] != scope or type(data['v']) is not int or data['v'] != 1
                    or not isinstance(data['id'], str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', data['id']) is None):
                raise ValueError()
            if versioned:
                # SQLAlchemy Integer maps to signed INT in MySQL. Do not coerce
                # floats, numeric strings or booleans into valid cursor values.
                value = data['value']
                if type(value) is not int or not 1 <= value <= 2**31 - 1:
                    raise ValueError()
            else:
                if not isinstance(data['value'], str):
                    raise ValueError()
                value = datetime.fromisoformat(data['value'])
                # Persisted timestamps are naive UTC; MySQL DATETIME starts at 1000.
                if value.tzinfo is not None or value.year < 1000 or value.isoformat() != data['value']:
                    raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
            raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None
        statement = statement.where(or_(column < value, and_(column == value, model.id < data['id'])))
    rows = db.scalars(statement.order_by(column.desc(), model.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        value = last.version if versioned else last.created_at.isoformat()
        next_cursor = base64.urlsafe_b64encode(json.dumps({'scope': scope, 'v': 1, 'value': value, 'id': last.id}).encode()).decode()
    return rows[:limit], next_cursor


def version_data(db, row):
    """Serialize one bounded draft or confirmed review queue."""
    return {**version_summary(row), 'questions': [question_data(q) for q in questions(db, row.id)]}


def get_evaluation_snapshot(db, project_id, version_id):
    """Return frozen inputs for an already authorized project; never accept a draft for evaluation."""
    row = db.scalar(select(TestSetVersion).join(TestSet).where(TestSet.project_id == project_id, TestSetVersion.id == version_id))
    if row is None:
        raise not_found()
    if row.status != 'confirmed' or row.snapshot is None:
        raise ApiError(409, 'TESTSET_NOT_CONFIRMED', '확정된 테스트셋 버전만 평가할 수 있습니다.')
    return row.snapshot
