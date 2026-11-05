from typing import Annotated

from fastapi import Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import ApiError
from app.models.auth import User
from app.models.project import Project
from app.services.auth import user_from_access

bearer = HTTPBearer(auto_error=False)
Db = Annotated[Session, Depends(get_db)]
ResourceId = Annotated[str, Path(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]


def get_current_user(db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> User:
    user = user_from_access(db, credentials.credentials) if credentials and credentials.scheme.lower() == 'bearer' else None
    if user is None:
        raise ApiError(401, 'UNAUTHORIZED', '인증이 필요합니다.', {'WWW-Authenticate': 'Bearer'})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_project_owner(projectId: ResourceId, user: CurrentUser, db: Db) -> Project:
    project = db.scalar(select(Project).where(Project.id == projectId, Project.owner_id == user.id))
    if project is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프로젝트를 찾을 수 없습니다.')
    return project


OwnedProject = Annotated[Project, Depends(require_project_owner)]
