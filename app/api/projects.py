import base64
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import AdminProject, CurrentUser, Db, OwnedProject, OwnedService, ResourceId
from app.core.errors import ApiError
from app.core.cursors import cursor_id, cursor_time, decode_cursor
from app.models.project import Chatbot, Project, ProjectMember
from app.models.connection import Connection
from app.schemas.project import ChatbotCreate, ProjectCreate, ChatbotPatch, ProjectPatch
from app.services.auth import now
from app.services.project_progress import project_progress

from app.schemas.responses import Data, Page, ProjectResponse, ChatbotResponse, ERROR_RESPONSES

router = APIRouter(prefix='/api/projects', tags=['projects'], responses=ERROR_RESPONSES)


def iso(value: datetime) -> str:
    """Serialize database timestamps as UTC ISO 8601 strings."""
    return value.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z')


def project_data(project: Project, service_count: int, progress=None) -> dict:
    """Serialize a project with its current chatbot registration count."""
    return {'id': project.id, 'name': project.name, 'description': project.description,
            'revision': project.revision, 'serviceCount': service_count,
            'createdAt': iso(project.created_at), 'updatedAt': iso(project.updated_at),
            **({'progress': progress} if progress is not None else {})}


def chatbot_data(service: Chatbot, has_connection: bool = False) -> dict:
    """Serialize chatbot registration and its current connection presence."""
    return {'id': service.id, 'projectId': service.project_id, 'name': service.name,
            'description': service.description, 'revision': service.revision,
            'hasConnection': has_connection, 'createdAt': iso(service.created_at), 'updatedAt': iso(service.updated_at)}


@router.post('', status_code=201, response_model=Data[ProjectResponse])
def create_project(body: ProjectCreate, user: CurrentUser, db: Db, response: Response):
    """Create a project owned by the authenticated user and return its resource URL."""
    current = now()
    project = Project(id=str(uuid4()), owner_id=user.id, name=body.name, description=body.description,
                      revision=1, created_at=current, updated_at=current)
    db.add(project)
    db.commit()
    # Read persisted precision (MySQL DATETIME) before issuing pagination cursors.
    db.refresh(project)
    response.headers['Location'] = f'/api/projects/{project.id}'
    return {'data': project_data(project, 0)}


@router.get('', response_model=Data[Page[ProjectResponse]])
def list_projects(user: CurrentUser, db: Db,
                  limit: Annotated[int, Query(ge=1, le=100)] = 20,
                  cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None):
    """Page through only the caller's projects using a stable timestamp and ID cursor."""
    has_service = exists().where(Chatbot.project_id == Project.id)
    membership = exists().where(ProjectMember.project_id == Project.id, ProjectMember.user_id == user.id,
                                ProjectMember.status == 'active')
    statement = select(Project, has_service).where(Project.deleted_at.is_(None), or_(Project.owner_id == user.id, membership))
    if cursor:
        try:
            payload = decode_cursor(cursor, {'v', 'owner', 'createdAt', 'id'})
            if type(payload['v']) is not int or payload['v'] != 1 or payload['owner'] != user.id:
                raise ValueError()
            created = cursor_time(payload['createdAt'])
            last_id = cursor_id(payload['id'])
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ApiError(400, 'INVALID_CURSOR', '유효하지 않은 커서입니다.') from None
        statement = statement.where(or_(Project.created_at < created,
                                       and_(Project.created_at == created, Project.id < last_id)))
    rows = db.execute(statement.order_by(Project.created_at.desc(), Project.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = base64.urlsafe_b64encode(json.dumps({'v': 1, 'owner': user.id,
            'createdAt': last.created_at.isoformat(), 'id': last.id}).encode()).decode()
    progress = project_progress(db, [p.id for p, _ in page])
    return {'data': {'items': [project_data(p, int(has), progress[p.id]) for p, has in page], 'nextCursor': next_cursor}}


@router.get('/{projectId}', response_model=Data[ProjectResponse])
def get_project(project: OwnedProject, db: Db):
    """Return a project after authentication and ownership have been checked."""
    has = db.scalar(select(exists().where(Chatbot.project_id == project.id)))
    return {'data': project_data(project, int(has), project_progress(db, [project.id])[project.id])}


def update_metadata(db, row, body):
    """Change only supplied metadata with optimistic concurrency protection."""
    model = type(row)
    values = body.model_dump(exclude_unset=True, exclude={'expectedRevision'})
    changed = db.execute(update(model).where(model.id == row.id, model.revision == body.expectedRevision)
        .values(**values, revision=model.revision + 1, updated_at=now())
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision으로 다시 요청해 주세요.')
    db.commit()
    db.refresh(row)


@router.patch('/{projectId}', response_model=Data[ProjectResponse])
def update_project(body: ProjectPatch, project: AdminProject, db: Db):
    """프로젝트 이름·설명을 수정합니다. 생략한 필드는 유지하며 description=null은 설명을 지웁니다."""
    update_metadata(db, project, body)
    return get_project(project, db)


@router.post('/{projectId}/services', status_code=201, tags=['services'], response_model=Data[ChatbotResponse])
def create_chatbot(body: ChatbotCreate, project: OwnedProject, db: Db, response: Response):
    """Register one chatbot, relying on the unique project constraint for concurrent requests."""
    current = now()
    service = Chatbot(id=str(uuid4()), project_id=project.id, name=body.name,
                      description=body.description, revision=1, created_at=current, updated_at=current)
    db.add(service)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(Chatbot.id).where(Chatbot.project_id == project.id)) is None:
            raise
        raise ApiError(409, 'SERVICE_ALREADY_EXISTS', '프로젝트에 챗봇이 이미 등록되어 있습니다.') from None
    db.refresh(service)
    response.headers['Location'] = f'/api/projects/{project.id}/services/{service.id}'
    return {'data': chatbot_data(service)}


@router.get('/{projectId}/services', tags=['services'], response_model=Data[Page[ChatbotResponse]])
def list_chatbots(project: OwnedProject, db: Db):
    """Return the zero-or-one chatbot belonging to an authorized project."""
    service = db.scalar(select(Chatbot).where(Chatbot.project_id == project.id))
    has_connection = bool(service and db.scalar(select(exists().where(Connection.service_id == service.id))))
    return {'data': {'items': [chatbot_data(service, has_connection)] if service else [], 'nextCursor': None}}


@router.get('/{projectId}/services/{serviceId}', tags=['services'], response_model=Data[ChatbotResponse])
def get_chatbot(serviceId: ResourceId, project: OwnedProject, db: Db):
    """Require both project ownership and matching chatbot membership."""
    service = db.scalar(select(Chatbot).where(Chatbot.id == serviceId, Chatbot.project_id == project.id))
    if service is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '챗봇을 찾을 수 없습니다.')
    has_connection = bool(db.scalar(select(exists().where(Connection.service_id == service.id))))
    return {'data': chatbot_data(service, has_connection)}


@router.patch('/{projectId}/services/{serviceId}', tags=['services'], response_model=Data[ChatbotResponse])
def update_chatbot(body: ChatbotPatch, service: OwnedService, project: AdminProject, db: Db):
    """챗봇 이름·설명을 수정합니다. expectedRevision은 챗봇 자체의 revision입니다."""
    update_metadata(db, service, body)
    has_connection = bool(db.scalar(select(exists().where(Connection.service_id == service.id))))
    return {'data': chatbot_data(service, has_connection)}
