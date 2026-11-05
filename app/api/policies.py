from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from sqlalchemy import select, update

from app.api.dependencies import IfMatch, CurrentUser, Db, OwnedProject, ResourceId
from app.core.errors import ApiError
from app.models.policy import Policy, PolicyVersion
from app.schemas.policy import PolicyCreate, PolicyVersionCreate, PolicyDraft, Name, PolicyImports
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.schemas.policy import PolicyOut, PolicyDetail, PolicyVersionOut
from app.services import policy as service
from app.services.auth import now
from app.services.policy_extraction import PolicyExtractor, extract_rules, get_policy_extractor
from app.services.policy_documents import extract_document_details, safe_filename, MAX_FILE_BYTES
from app.services.rate_limit import check_rate_limit

Extractor = Annotated[PolicyExtractor, Depends(get_policy_extractor)]

router = APIRouter(prefix='/api/projects/{projectId}/policies', tags=['policies'], responses=ERROR_RESPONSES)
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=2048)]


def check_extraction_limit(db, user):
    retry_after = check_rate_limit(db, user.id, 'policy-extraction', 10)
    if retry_after is not None:
        raise ApiError(429, 'RATE_LIMITED', '규칙 추출 요청이 너무 많습니다.', headers={'Retry-After': str(retry_after)})


def make_draft(name, content, extractor, source=None):
    rules = extract_rules(extractor, content)
    return PolicyDraft(name=name, content=content, rules=rules, sourceDocument=source)


@router.post('', status_code=201, response_model=Data[PolicyDetail | PolicyDraft],
    responses={200: {'model': Data[PolicyDraft], 'description': 'rules 생략 시 미저장 규칙 초안'}})
def create_policy(body: PolicyCreate, project: OwnedProject, user: CurrentUser, db: Db, response: Response, extractor: Extractor):
    """Save an original policy and its first immutable human-authored rule version."""
    if body.rules is None:
        check_extraction_limit(db, user)
        response.status_code = 200
        return {'data': make_draft(body.name, body.content, extractor, body.sourceDocument)}
    row = Policy(id=service.uid(), project_id=project.id, name=body.name, revision=1, deleted=False, created_at=now())
    db.add(row)
    db.flush()
    version = PolicyVersion(id=service.uid(), policy_id=row.id, version=1, content=body.content,
                            rules=[rule.model_dump() for rule in body.rules],
                            source_document=body.sourceDocument.model_dump() if body.sourceDocument else None, created_at=now())
    db.add(version)
    db.commit()
    db.refresh(row)
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{project.id}/policies/{row.id}'
    return {'data': {'policy': service.policy_data(row), 'version': service.policy_version_data(version)}}


def import_file(file, name, ocr_mode, extractor, db, user):
    check_extraction_limit(db, user)
    content, source = extract_document_details(file.file.read(MAX_FILE_BYTES + 1), file.filename, ocr_mode)
    return make_draft(name, content, extractor, source)


@router.post('/import', response_model=Data[PolicyDraft])
def import_policy(project: OwnedProject, user: CurrentUser, db: Db, extractor: Extractor,
                  file: Annotated[UploadFile, File(description='10MiB 이하 PDF/DOCX/UTF-8 TXT/MD. PDF 최대 50페이지, 한국어·영어 OCR 지원')],
                  name: Annotated[Name, Form()],
                  ocrMode: Annotated[Literal['auto', 'force'], Form()] = 'auto'):
    """검토 후 name/content/rules/sourceDocument를 정책 생성 API로 보내 저장하세요."""
    try:
        return {'data': import_file(file, name, ocrMode, extractor, db, user)}
    finally:
        file.file.close()


@router.post('/imports', response_model=Data[PolicyImports])
def import_policies(project: OwnedProject, user: CurrentUser, db: Db, extractor: Extractor,
                    files: Annotated[list[UploadFile], File(min_length=1, max_length=5)],
                    ocrMode: Annotated[Literal['auto', 'force'], Form()] = 'auto'):
    """최대 5개 파일을 순서대로 처리하고 각 파일의 초안 또는 오류를 반환합니다. 저장하지 않습니다."""
    items = []
    try:
        for file in files:
            filename = safe_filename(file.filename)
            try:
                draft = import_file(file, Path(filename).stem[:100].strip() or '정책 문서', ocrMode, extractor, db, user)
                items.append({'filename': filename, 'status': 'draft', 'draft': draft})
            except ApiError as exc:
                items.append({'filename': filename, 'status': 'failed', 'error': {
                    'code': exc.code, 'message': exc.message, 'httpStatus': exc.status,
                    'retryAfter': (exc.headers or {}).get('Retry-After')}})
            finally:
                file.file.close()
    finally:
        for file in files:
            file.file.close()
    return {'data': {'items': items}}


@router.get('', response_model=Data[Page[PolicyOut]])
def list_policies(project: OwnedProject, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Page through active policies belonging to the authorized project."""
    rows, next_cursor = service.paginate(db, select(Policy).where(Policy.project_id == project.id, Policy.deleted.is_(False)),
        Policy, f'policies:{project.id}', limit, cursor)
    return {'data': {'items': [service.policy_data(row) for row in rows], 'nextCursor': next_cursor}}


@router.get('/{policyId}', response_model=Data[PolicyDetail])
def get_policy(policyId: ResourceId, project: OwnedProject, db: Db):
    """Return an active policy and its latest stored version."""
    row = service.policy_row(db, project.id, policyId)
    version = db.scalar(select(PolicyVersion).where(PolicyVersion.policy_id == row.id).order_by(PolicyVersion.version.desc()))
    return {'data': {'policy': service.policy_data(row), 'version': service.policy_version_data(version)}}


@router.post('/{policyId}/versions', status_code=201, response_model=Data[PolicyVersionOut])
def create_version(policyId: ResourceId, body: PolicyVersionCreate, project: OwnedProject, db: Db, response: Response):
    """Allocate a new immutable policy version with optimistic conflict protection."""
    row = service.policy_row(db, project.id, policyId)
    result = db.execute(update(Policy).where(Policy.id == row.id, Policy.revision == body.expectedRevision,
        Policy.deleted.is_(False)).values(revision=Policy.revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision으로 다시 요청해 주세요.')
    db.refresh(row)
    version = PolicyVersion(id=service.uid(), policy_id=row.id, version=row.revision, content=body.content,
                            rules=[rule.model_dump() for rule in body.rules],
                            source_document=body.sourceDocument.model_dump() if body.sourceDocument else None, created_at=now())
    db.add(version)
    db.commit()
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{project.id}/policies/{row.id}/versions/{version.id}'
    return {'data': service.policy_version_data(version)}


@router.get('/{policyId}/versions', response_model=Data[Page[PolicyVersionOut]])
def list_versions(policyId: ResourceId, project: OwnedProject, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Return policy history in descending version order."""
    row = service.policy_row(db, project.id, policyId)
    rows, next_cursor = service.paginate(db, select(PolicyVersion).where(PolicyVersion.policy_id == row.id),
        PolicyVersion, f'policyVersions:{row.id}', limit, cursor, versioned=True)
    return {'data': {'items': [service.policy_version_data(item) for item in rows], 'nextCursor': next_cursor}}


@router.get('/{policyId}/versions/{policyVersionId}', response_model=Data[PolicyVersionOut])
def get_version(policyId: ResourceId, policyVersionId: ResourceId, project: OwnedProject, db: Db):
    """Require both policy ownership and correct version membership."""
    service.policy_row(db, project.id, policyId)
    row = db.scalar(select(PolicyVersion).where(PolicyVersion.id == policyVersionId, PolicyVersion.policy_id == policyId))
    if row is None:
        raise service.not_found()
    return {'data': service.policy_version_data(row)}


@router.delete('/{policyId}', status_code=204)
def delete_policy(policyId: ResourceId, project: OwnedProject, db: Db, if_match: IfMatch = None):
    """Logically delete a policy while preserving all evidence used by historical test sets."""
    row = service.policy_row(db, project.id, policyId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{row.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인해 주세요.')
    result = db.execute(update(Policy).where(Policy.id == row.id, Policy.revision == row.revision,
        Policy.deleted.is_(False)).values(deleted=True, revision=Policy.revision + 1))
    if result.rowcount != 1:
        db.rollback()
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인해 주세요.')
    db.commit()
    return Response(status_code=204)
