from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import AUTH_LOGIN_RATE_LIMIT, AUTH_REFRESH_RATE_LIMIT
from app.core.database import get_db
from app.core.errors import ApiError
from app.api.dependencies import get_current_user
from app.models.auth import User
from app.schemas.auth import LoginId, LoginRequest, RefreshRequest, SignupRequest
from app.services import auth, rate_limit

from app.schemas.responses import Data, UserResponse, Availability, LoginResponse, TokenPair, ERROR_RESPONSES

router = APIRouter(prefix='/api/auth', tags=['auth'], responses=ERROR_RESPONSES)
Db = Annotated[Session, Depends(get_db)]


def invalid_refresh() -> ApiError:
    return ApiError(401, 'INVALID_REFRESH_TOKEN', '유효하지 않은 리프레시 토큰입니다.')


@router.post('/signup', status_code=201, response_model=Data[UserResponse])
def signup(body: SignupRequest, response: Response, db: Db):
    user = User(id=str(uuid4()), login_id=body.loginId, name=body.name,
                password_hash=auth.password_hash.hash(body.password), created_at=auth.now())
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(User.id).where(User.login_id == body.loginId)) is None:
            raise
        raise ApiError(409, 'LOGIN_ID_ALREADY_EXISTS', '이미 사용 중인 아이디입니다.')
    response.headers['Location'] = '/api/auth/me'
    return {'data': auth.user_data(user)}


@router.get('/login-id-availability', response_model=Data[Availability])
def login_id_availability(db: Db, loginId: Annotated[LoginId, Query()]):
    exists = db.scalar(select(User.id).where(User.login_id == loginId)) is not None
    return {'data': {'loginId': loginId, 'available': not exists}}


def enforce_rate_limit(request: Request, db: Session, route: str, limit: int):
    # Use the server-resolved client, never an untrusted forwarded header directly.
    client = request.client.host if request.client else 'unknown'
    retry = rate_limit.check_rate_limit(db, client, route, limit)
    if retry is not None:
        raise ApiError(429, 'RATE_LIMITED', '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.', {'Retry-After': str(retry)})


@router.post('/login', response_model=Data[LoginResponse])
def login(body: LoginRequest, db: Db, request: Request):
    enforce_rate_limit(request, db, 'login', AUTH_LOGIN_RATE_LIMIT)
    user = db.scalar(select(User).where(User.login_id == body.loginId))
    valid = auth.password_hash.verify(body.password, user.password_hash if user else auth.DUMMY_HASH)
    if user is None or not valid:
        raise ApiError(401, 'INVALID_CREDENTIALS', '아이디 또는 비밀번호를 확인해 주세요.')
    return {'data': {'user': auth.user_data(user), **auth.create_session(db, user)}}


@router.get('/me', response_model=Data[UserResponse])
def me(user: Annotated[User, Depends(get_current_user)]):
    return {'data': auth.user_data(user)}


@router.post('/refresh', response_model=Data[TokenPair])
def refresh(body: RefreshRequest, db: Db, request: Request):
    enforce_rate_limit(request, db, 'refresh', AUTH_REFRESH_RATE_LIMIT)
    tokens = auth.rotate(db, body.refreshToken)
    if tokens is None:
        raise invalid_refresh()
    return {'data': tokens}


@router.post('/logout', status_code=204)
def logout(body: RefreshRequest, db: Db):
    if not auth.revoke(db, body.refreshToken):
        raise invalid_refresh()
    return Response(status_code=204)
