from fastapi import APIRouter, Response
from sqlalchemy import func, select

from app.api.dependencies import AdminProject, CurrentUser, Db, IfMatch, OwnedProject, ResourceId
from app.core.errors import ApiError
from app.models.auth import User
from app.models.project import Project, ProjectMember
from app.schemas.membership import InvitationCreate, InvitationOut, MemberOut, MemberPatch
from app.schemas.policy import Revision
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.services.auth import now
from app.services.policy import iso, uid

router = APIRouter(prefix='/api', tags=['members'], responses=ERROR_RESPONSES)


def member_data(user, row=None):
    return {'userId': user.id, 'loginId': user.login_id, 'name': user.name,
        'role': row.role if row else 'admin', 'status': row.status if row else 'owner',
        'revision': row.revision if row else 1, 'lastSeenAt': iso(user.last_seen_at)}


@router.get('/projects/{projectId}/members', response_model=Data[Page[MemberOut]])
def list_members(project: OwnedProject, db: Db):
    rows = db.execute(select(User, ProjectMember).join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project.id).order_by(ProjectMember.created_at, ProjectMember.id)).all()
    return {'data': {'items': [member_data(db.get(User, project.owner_id))] + [member_data(user, row) for user, row in rows], 'nextCursor': None}}


@router.post('/projects/{projectId}/invitations', status_code=201, response_model=Data[InvitationOut])
def invite(body: InvitationCreate, project: AdminProject, user: CurrentUser, db: Db):
    db.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    target = db.scalar(select(User).where(User.login_id == body.loginId))
    if target is None:
        raise ApiError(404, 'USER_NOT_FOUND', '가입된 로그인 ID를 확인하세요.')
    if target.id == project.owner_id or db.scalar(select(ProjectMember.id).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == target.id).with_for_update()):
        raise ApiError(409, 'MEMBER_ALREADY_EXISTS', '이미 멤버이거나 초대 중입니다.')
    if db.scalar(select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id == project.id).with_for_update()) >= 199:
        raise ApiError(409, 'MEMBER_LIMIT', '프로젝트 멤버와 초대는 소유자를 포함해 최대 200명입니다.')
    row = ProjectMember(id=uid(), project_id=project.id, user_id=target.id, invited_by=user.id,
        role=body.role, status='invited', revision=1, created_at=now())
    db.add(row)
    db.commit()
    return {'data': {'id': row.id, 'projectId': project.id, 'projectName': project.name, 'role': row.role, 'revision': row.revision}}


@router.get('/invitations', response_model=Data[Page[InvitationOut]])
def my_invitations(user: CurrentUser, db: Db):
    rows = db.execute(select(ProjectMember, Project).join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.user_id == user.id, ProjectMember.status == 'invited', Project.deleted_at.is_(None))).all()
    return {'data': {'items': [{'id': row.id, 'projectId': project.id, 'projectName': project.name,
        'role': row.role, 'revision': row.revision} for row, project in rows], 'nextCursor': None}}


@router.post('/invitations/{invitationId}/accept', response_model=Data[MemberOut])
def accept(invitationId: ResourceId, body: Revision, user: CurrentUser, db: Db):
    row = db.scalar(select(ProjectMember).join(Project).where(ProjectMember.id == invitationId,
        ProjectMember.user_id == user.id, Project.deleted_at.is_(None)).with_for_update())
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '초대를 찾을 수 없습니다.')
    if row.status != 'invited' or row.revision != body.expectedRevision:
        raise ApiError(409, 'REVISION_CONFLICT', '최신 초대 상태를 확인하세요.')
    row.status, row.accepted_at, row.revision = 'active', now(), row.revision + 1
    db.commit()
    return {'data': member_data(user, row)}


def editable_member(db, project, user_id):
    if project.owner_id == user_id:
        raise ApiError(409, 'OWNER_PROTECTED', '프로젝트 소유자는 제거하거나 역할을 변경할 수 없습니다.')
    row = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id,
        ProjectMember.user_id == user_id).with_for_update())
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '멤버를 찾을 수 없습니다.')
    return row


@router.patch('/projects/{projectId}/members/{userId}', response_model=Data[MemberOut])
def change_role(userId: ResourceId, body: MemberPatch, project: AdminProject, db: Db):
    row = editable_member(db, project, userId)
    if row.revision != body.expectedRevision:
        raise ApiError(409, 'REVISION_CONFLICT', '최신 멤버 revision을 확인하세요.')
    row.role, row.revision = body.role, row.revision + 1
    db.commit()
    return {'data': member_data(db.get(User, userId), row)}


@router.delete('/projects/{projectId}/members/{userId}', status_code=204)
def remove_member(userId: ResourceId, project: AdminProject, db: Db, if_match: IfMatch = None):
    row = editable_member(db, project, userId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{row.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 멤버 revision을 확인하세요.')
    db.delete(row)
    db.commit()
    return Response(status_code=204)
