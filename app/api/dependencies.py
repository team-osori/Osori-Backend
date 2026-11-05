from typing import Annotated

from fastapi import Depends, Header, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import ApiError
from app.models.auth import User
from app.models.project import Chatbot, Project
from app.services.auth import user_from_access

bearer = HTTPBearer(auto_error=False)
Db = Annotated[Session, Depends(get_db)]
ResourceId = Annotated[str, Path(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]
IfMatch = Annotated[str | None, Header(alias='If-Match', max_length=30,
    description='따옴표로 감싼 최신 revision (예: "3"). 생략하면 428, 불일치하면 412입니다.',
    json_schema_extra={'pattern': r'^"[1-9][0-9]*"$'})]


def get_current_user(db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> User:
    """Reject missing, revoked, or expired bearer credentials before accessing protected data."""
    user = user_from_access(db, credentials.credentials) if credentials and credentials.scheme.lower() == 'bearer' else None
    if user is None:
        raise ApiError(401, 'UNAUTHORIZED', '인증이 필요합니다.', {'WWW-Authenticate': 'Bearer'})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_project_owner(projectId: ResourceId, user: CurrentUser, db: Db) -> Project:
    """Hide missing and other-owned projects behind the same not-found response."""
    project = db.scalar(select(Project).where(Project.id == projectId, Project.owner_id == user.id))
    if project is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프로젝트를 찾을 수 없습니다.')
    return project


OwnedProject = Annotated[Project, Depends(require_project_owner)]


def require_service_owner(serviceId: ResourceId, project: OwnedProject, db: Db) -> Chatbot:
    service = db.scalar(select(Chatbot).where(Chatbot.id == serviceId, Chatbot.project_id == project.id))
    if service is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '챗봇을 찾을 수 없습니다.')
    return service


OwnedService = Annotated[Chatbot, Depends(require_service_owner)]
