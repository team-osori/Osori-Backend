from datetime import timedelta

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import AdminProject, CurrentUser, Db, IfMatch, ResourceId
from app.core.errors import ApiError
from app.models.project import Project
from app.schemas.policy import Revision
from app.schemas.responses import Data, Page, ProjectResponse, ERROR_RESPONSES
from app.services.auth import now
from app.services.policy import iso
from app.services.project_lifecycle import RETENTION_DAYS, trash_project
from app.api.projects import get_project


class TrashProject(BaseModel):
    id: str
    name: str
    description: str | None
    revision: int
    createdAt: str
    updatedAt: str
    deletedAt: str
    purgeAfter: str


router = APIRouter(prefix='/api/projects', tags=['projects'], responses=ERROR_RESPONSES)


@router.get('/trash', response_model=Data[Page[TrashProject]])
def list_trash(user: CurrentUser, db: Db):
    from app.api.projects import project_data
    rows = db.scalars(select(Project).where(Project.owner_id == user.id, Project.deleted_at.is_not(None))
        .order_by(Project.deleted_at.desc(), Project.id)).all()
    return {'data': {'items': [{**project_data(row, 0), 'deletedAt': iso(row.deleted_at),
        'purgeAfter': iso(row.deleted_at + timedelta(days=RETENTION_DAYS))} for row in rows], 'nextCursor': None}}


@router.delete('/{projectId}', status_code=204)
def delete_project(project: AdminProject, db: Db, if_match: IfMatch = None):
    row = db.scalar(select(Project).where(Project.id == project.id).with_for_update().execution_options(populate_existing=True))
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if row.deleted_at or if_match != f'"{row.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인하세요.')
    trash_project(db, row)
    db.commit()
    return Response(status_code=204)


@router.post('/{projectId}/restore', response_model=Data[ProjectResponse])
def restore_project(projectId: ResourceId, body: Revision, user: CurrentUser, db: Db):
    row = db.scalar(select(Project).where(Project.id == projectId, Project.owner_id == user.id,
        Project.deleted_at.is_not(None)).with_for_update())
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '휴지통 프로젝트를 찾을 수 없습니다.')
    if row.deleted_at.replace(tzinfo=now().tzinfo) <= now() - timedelta(days=RETENTION_DAYS):
        raise ApiError(409, 'RESTORE_EXPIRED', '30일 복구 기한이 지났습니다.')
    if row.revision != body.expectedRevision:
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision을 확인하세요.')
    row.deleted_at, row.updated_at, row.revision = None, now(), row.revision + 1
    db.commit()
    db.refresh(row)
    return get_project(row, db)
