from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import select, update

from app.api.dependencies import Db, OwnedProject, ResourceId
from app.core.errors import ApiError
from app.models.policy import Policy, PolicyVersion
from app.schemas.policy import PolicyCreate, PolicyVersionCreate
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.schemas.policy import PolicyOut, PolicyDetail, PolicyVersionOut
from app.services import policy as service
from app.services.auth import now

router = APIRouter(prefix='/api/projects/{projectId}/policies', tags=['policies'], responses=ERROR_RESPONSES)
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=2048)]


@router.post('', status_code=201, response_model=Data[PolicyDetail])
def create_policy(body: PolicyCreate, project: OwnedProject, db: Db, response: Response):
    """Save an original policy and its first immutable human-authored rule version."""
    row = Policy(id=service.uid(), project_id=project.id, name=body.name, revision=1, deleted=False, created_at=now())
    db.add(row)
    db.flush()
    version = PolicyVersion(id=service.uid(), policy_id=row.id, version=1, content=body.content,
                            rules=[rule.model_dump() for rule in body.rules], created_at=now())
    db.add(version)
    db.commit()
    db.refresh(row)
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{project.id}/policies/{row.id}'
    return {'data': {'policy': service.policy_data(row), 'version': service.policy_version_data(version)}}


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
                            rules=[rule.model_dump() for rule in body.rules], created_at=now())
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
def delete_policy(policyId: ResourceId, project: OwnedProject, db: Db, if_match: Annotated[str | None, Header()] = None):
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
