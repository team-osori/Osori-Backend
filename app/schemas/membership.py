from typing import Literal

from pydantic import BaseModel
from app.schemas.auth import LoginId
from app.schemas.policy import Input, Revision


class InvitationCreate(Input):
    loginId: LoginId
    role: Literal['admin', 'member'] = 'member'


class MemberPatch(Revision):
    role: Literal['admin', 'member']


class MemberOut(BaseModel):
    userId: str
    loginId: str
    name: str
    role: Literal['admin', 'member']
    status: Literal['owner', 'active', 'invited']
    revision: int
    email: None = None
    lastSeenAt: str | None


class InvitationOut(BaseModel):
    id: str
    projectId: str
    projectName: str
    role: Literal['admin', 'member']
    revision: int


class ProjectRole(BaseModel):
    projectId: str
    role: Literal['admin', 'member']
