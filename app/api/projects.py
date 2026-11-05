import base64
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, Db, OwnedProject, ResourceId
from app.core.errors import ApiError
from app.models.project import Chatbot, Project
from app.schemas.project import ChatbotCreate, ProjectCreate
from app.services.auth import now

from app.schemas.responses import Data, Page, ProjectResponse, ChatbotResponse, ERROR_RESPONSES

router = APIRouter(prefix='/api/projects', tags=['projects'], responses=ERROR_RESPONSES)


def iso(value: datetime) -> str:
    """Serialize database timestamps as UTC ISO 8601 strings."""
    return value.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z')


def project_data(project: Project, service_count: int) -> dict:
    """Serialize a project with its current chatbot registration count."""
    return {'id': project.id, 'name': project.name, 'description': project.description,
            'revision': project.revision, 'serviceCount': service_count,
            'createdAt': iso(project.created_at), 'updatedAt': iso(project.updated_at)}


def chatbot_data(service: Chatbot) -> dict:
    """Serialize chatbot registration; connection setup belongs to its separate feature."""
    return {'id': service.id, 'projectId': service.project_id, 'name': service.name,
            'description': service.description, 'revision': service.revision,
            'hasConnection': False, 'createdAt': iso(service.created_at), 'updatedAt': iso(service.updated_at)}


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
    statement = select(Project, has_service).where(Project.owner_id == user.id)
    if cursor:
        try:
            payload = json.loads(base64.b64decode(cursor.encode(), altchars=b'-_', validate=True))
            if payload['v'] != 1 or payload['owner'] != user.id:
                raise ValueError()
            created = datetime.fromisoformat(payload['createdAt'])
            last_id = payload['id']
            if not isinstance(last_id, str) or len(last_id) > 100:
                raise ValueError()
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
    return {'data': {'items': [project_data(p, int(has)) for p, has in page], 'nextCursor': next_cursor}}


@router.get('/{projectId}', response_model=Data[ProjectResponse])
def get_project(project: OwnedProject, db: Db):
    """Return a project after authentication and ownership have been checked."""
    has = db.scalar(select(exists().where(Chatbot.project_id == project.id)))
    return {'data': project_data(project, int(has))}


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
    return {'data': {'items': [chatbot_data(service)] if service else [], 'nextCursor': None}}


@router.get('/{projectId}/services/{serviceId}', tags=['services'], response_model=Data[ChatbotResponse])
def get_chatbot(serviceId: ResourceId, project: OwnedProject, db: Db):
    """Require both project ownership and matching chatbot membership."""
    service = db.scalar(select(Chatbot).where(Chatbot.id == serviceId, Chatbot.project_id == project.id))
    if service is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '챗봇을 찾을 수 없습니다.')
    return {'data': chatbot_data(service)}
