from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import AUTH_LOGIN_RATE_LIMIT, AUTH_REFRESH_RATE_LIMIT, AUTH_SIGNUP_RATE_LIMIT
from app.core.database import get_db
from app.core.errors import ApiError
from app.api.dependencies import get_current_user
from app.models.auth import User
from app.models.project import Project, ProjectMember
from app.schemas.membership import ProjectRole
from app.schemas.auth import Email, LegacyEmailLink, LoginRequest, RefreshRequest, SignupRequest
from app.services import auth, rate_limit

from app.schemas.responses import Data, UserResponse, Availability, LoginResponse, TokenPair, ERROR_RESPONSES


class MeResponse(UserResponse):
    projectRoles: list[ProjectRole]

router = APIRouter(prefix='/api/auth', tags=['auth'], responses=ERROR_RESPONSES)
Db = Annotated[Session, Depends(get_db)]


def invalid_refresh() -> ApiError:
    """Build a uniform error for unknown, expired, replayed, or exhausted refresh tokens."""
    return ApiError(401, 'INVALID_REFRESH_TOKEN', '유효하지 않은 리프레시 토큰입니다.')


@router.post('/signup', status_code=201, response_model=Data[UserResponse])
def signup(body: SignupRequest, response: Response, db: Db, request: Request):
    """Throttle before hashing and create a user with a normalized unique email."""
    enforce_rate_limit(request, db, 'signup', AUTH_SIGNUP_RATE_LIMIT)
    user = User(id=str(uuid4()), login_id='u_' + uuid4().hex[:28], email=body.email, name=body.name,
                password_hash=auth.password_hash.hash(body.password), created_at=auth.now())
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(User.id).where(User.email == body.email)) is None:
            raise
        raise ApiError(409, 'EMAIL_ALREADY_EXISTS', '이미 사용 중인 이메일입니다.') from None
    response.headers['Location'] = '/api/auth/me'
    return {'data': auth.user_data(user)}


@router.get('/email-availability', response_model=Data[Availability])
def email_availability(db: Db, email: Annotated[Email, Query()]):
    """Check whether a normalized email is available."""
    exists = db.scalar(select(User.id).where(User.email == email)) is not None
    return {'data': {'email': email, 'available': not exists}}


@router.post('/legacy-email-link', response_model=Data[UserResponse])
def link_legacy_email(body: LegacyEmailLink, db: Db, request: Request):
    """Attach an email once after authenticating the original legacy account."""
    enforce_rate_limit(request, db, 'login', AUTH_LOGIN_RATE_LIMIT)
    user = db.scalar(select(User).where(User.login_id == body.loginId, User.email.is_(None)))
    valid = auth.password_hash.verify(body.password, user.password_hash if user else auth.DUMMY_HASH)
    if user is None or not valid:
        raise ApiError(401, 'INVALID_CREDENTIALS', '기존 계정 정보가 올바르지 않습니다.')
    try:
        changed = db.execute(update(User).where(User.id == user.id, User.email.is_(None))
            .values(email=body.email).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback()
            raise ApiError(409, 'EMAIL_LINK_CONFLICT', '이미 이메일이 연결된 계정입니다.')
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(User.id).where(User.email == body.email)) is None:
            raise
        raise ApiError(409, 'EMAIL_ALREADY_EXISTS', '이미 사용 중인 이메일입니다.') from None
    db.refresh(user)
    return {'data': auth.user_data(user)}


def enforce_rate_limit(request: Request, db: Session, route: str, limit: int):
    # Use the server-resolved client, never an untrusted forwarded header directly.
    """Apply a shared client-IP budget and report the remaining retry interval."""
    client = request.client.host if request.client else 'unknown'
    retry = rate_limit.check_rate_limit(db, client, route, limit)
    if retry is not None:
        raise ApiError(429, 'RATE_LIMITED', '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.', {'Retry-After': str(retry)})


@router.post('/login', response_model=Data[LoginResponse])
def login(body: LoginRequest, db: Db, request: Request):
    """Verify credentials uniformly and issue a new independent login session."""
    enforce_rate_limit(request, db, 'login', AUTH_LOGIN_RATE_LIMIT)
    user = db.scalar(select(User).where(User.email == body.email))
    valid = auth.password_hash.verify(body.password, user.password_hash if user else auth.DUMMY_HASH)
    if user is None or not valid:
        raise ApiError(401, 'INVALID_CREDENTIALS', '이메일 또는 비밀번호를 확인해 주세요.')
    tokens = auth.create_session(db, user)
    db.refresh(user)
    return {'data': {'user': auth.user_data(user), **tokens}}


@router.get('/me', response_model=Data[MeResponse])
def me(user: Annotated[User, Depends(get_current_user)], db: Db):
    """Return only public information for the authenticated user."""
    rows = db.execute(select(Project, ProjectMember.role).outerjoin(ProjectMember,
        (ProjectMember.project_id == Project.id) & (ProjectMember.user_id == user.id) & (ProjectMember.status == 'active'))
        .where(Project.deleted_at.is_(None), or_(Project.owner_id == user.id, ProjectMember.id.is_not(None)))).all()
    return {'data': {**auth.user_data(user), 'projectRoles': [{'projectId': p.id,
        'role': 'admin' if p.owner_id == user.id else role} for p, role in rows]}}


@router.post('/refresh', response_model=Data[TokenPair])
def refresh(body: RefreshRequest, db: Db, request: Request):
    """Apply the refresh budget before performing token rotation or replay detection."""
    enforce_rate_limit(request, db, 'refresh', AUTH_REFRESH_RATE_LIMIT)
    tokens = auth.rotate(db, body.refreshToken)
    if tokens is None:
        raise invalid_refresh()
    return {'data': tokens}


@router.post('/logout', status_code=204)
def logout(body: RefreshRequest, db: Db):
    """Invalidate both tokens of the session authenticated by its refresh token."""
    if not auth.revoke(db, body.refreshToken):
        raise invalid_refresh()
    return Response(status_code=204)
