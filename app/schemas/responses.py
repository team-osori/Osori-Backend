from typing import Generic, TypeVar
from pydantic import BaseModel, Field
from app.schemas.project import ProjectProgress

T = TypeVar('T')


class Data(BaseModel, Generic[T]):
    data: T


class Page(BaseModel, Generic[T]):
    items: list[T]
    nextCursor: str | None


class UserResponse(BaseModel):
    id: str
    loginId: str
    name: str
    createdAt: str
    email: None = None
    lastSeenAt: str | None = None


class TokenPair(BaseModel):
    accessToken: str
    refreshToken: str


class LoginResponse(TokenPair):
    user: UserResponse


class Availability(BaseModel):
    loginId: str
    available: bool


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    revision: int
    serviceCount: int
    createdAt: str
    updatedAt: str
    progress: ProjectProgress = Field(default_factory=ProjectProgress)


class ChatbotResponse(BaseModel):
    id: str
    projectId: str
    name: str
    description: str | None
    revision: int
    hasConnection: bool
    createdAt: str
    updatedAt: str


class FieldError(BaseModel):
    field: str
    code: str
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    requestId: str
    fieldErrors: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


ERROR_RESPONSES = {status: {'model': ErrorResponse} for status in (400, 401, 403, 404, 409, 412, 413, 415, 428, 429, 500, 502, 503, 504)}
