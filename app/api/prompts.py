from uuid import uuid4

from fastapi import APIRouter, Response
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.api.dependencies import Db, OwnedService, ResourceId
from app.api.projects import iso
from app.core.errors import ApiError
from app.core.concurrency import is_write_conflict
from app.core.pagination import Cursor, Limit, page_query
from app.models.prompt import Prompt, PromptVersion
from app.schemas.prompt import (PromptCreate, PromptResponse, PromptSummary,
    PromptVersionCreate, PromptVersionResponse, PromptVersionSummary)
from app.schemas.responses import Data, ERROR_RESPONSES, Page
from app.services.auth import now
from app.services.prompts import get_prompt, get_version
from app.services.version_pagination import version_page

router = APIRouter(prefix='/api/projects/{projectId}/services/{serviceId}/prompts',
                   tags=['prompts'], responses=ERROR_RESPONSES)


def version_data(v: PromptVersion) -> dict:
    return {'id': v.id, 'promptId': v.prompt_id, 'version': v.version, 'content': v.content,
            'label': v.label, 'changeNote': v.change_note, 'externalVersionRef': v.external_version_ref,
            'createdAt': iso(v.created_at)}


def prompt_data(p: Prompt, v: PromptVersion) -> dict:
    return {'id': p.id, 'serviceId': p.service_id, 'name': p.name, 'revision': p.revision,
            'currentVersionId': p.current_version_id, 'currentVersion': version_data(v),
            'createdAt': iso(p.created_at), 'updatedAt': iso(p.updated_at)}


def new_version(body, prompt_id, number, version_id=None):
    return PromptVersion(id=version_id or str(uuid4()), prompt_id=prompt_id, version=number,
        content=body.content, label=body.label, change_note=body.changeNote,
        external_version_ref=body.externalVersionRef, created_at=now())


@router.post('', status_code=201, response_model=Data[PromptResponse])
def create_prompt(body: PromptCreate, service: OwnedService, db: Db, response: Response):
    current = now()
    prompt = Prompt(id=str(uuid4()), service_id=service.id, name=body.name, revision=1,
        current_version_id=str(uuid4()), created_at=current, updated_at=current)
    db.add(prompt)
    db.flush()
    version = new_version(body, prompt.id, 1, prompt.current_version_id)
    db.add(version)
    db.commit()
    db.refresh(prompt)
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{service.project_id}/services/{service.id}/prompts/{prompt.id}'
    return {'data': prompt_data(prompt, version)}


@router.get('', response_model=Data[Page[PromptSummary]])
def list_prompts(service: OwnedService, db: Db, limit: Limit = 20, cursor: Cursor = None):
    rows, next_cursor = page_query(db, select(Prompt).where(Prompt.service_id == service.id),
                                  Prompt, f'prompts:{service.id}', limit, cursor)
    return {'data': {'items': [{'id': p.id, 'name': p.name, 'currentVersionId': p.current_version_id,
        'version': p.revision, 'revision': p.revision, 'createdAt': iso(p.created_at),
        'updatedAt': iso(p.updated_at)} for p in rows], 'nextCursor': next_cursor}}


@router.get('/{promptId}', response_model=Data[PromptResponse])
def prompt_detail(promptId: ResourceId, service: OwnedService, db: Db):
    prompt = get_prompt(db, service.id, promptId)
    return {'data': prompt_data(prompt, get_version(db, service.id, prompt.current_version_id))}


@router.post('/{promptId}/versions', status_code=201, response_model=Data[PromptVersionResponse])
def create_version(promptId: ResourceId, body: PromptVersionCreate, service: OwnedService,
                   db: Db, response: Response):
    prompt = get_prompt(db, service.id, promptId)
    if prompt.current_version_id != body.baseVersionId:
        raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '최신 버전을 기준으로 저장해 주세요.')
    previous = get_version(db, service.id, prompt.current_version_id)
    if previous.content == body.content and previous.external_version_ref == body.externalVersionRef:
        raise ApiError(409, 'NO_CHANGES', '원문 또는 외부 버전 참조를 변경해 주세요.')
    version = new_version(body, prompt.id, previous.version + 1)
    try:
        changed = db.execute(update(Prompt).where(Prompt.id == prompt.id,
            Prompt.current_version_id == body.baseVersionId).values(current_version_id=version.id,
            revision=Prompt.revision + 1, updated_at=now()))
    except OperationalError as exc:
        db.rollback()
        if is_write_conflict(exc):
            raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '다른 수정본이 먼저 저장되었습니다.') from None
        raise
    if changed.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '다른 수정본이 먼저 저장되었습니다.')
    db.add(version)
    db.commit()
    db.refresh(version)
    response.headers['Location'] = f'/api/projects/{service.project_id}/services/{service.id}/prompts/{prompt.id}/versions/{version.id}'
    return {'data': version_data(version)}


@router.get('/{promptId}/versions', response_model=Data[Page[PromptVersionSummary]])
def list_versions(promptId: ResourceId, service: OwnedService, db: Db,
                  limit: Limit = 20, cursor: Cursor = None):
    get_prompt(db, service.id, promptId)
    # Version number is the ordering key: timestamps may have second precision on MySQL.
    rows, next_cursor = version_page(db, promptId, limit, cursor)
    return {'data': {'items': [version_data(v) for v in rows], 'nextCursor': next_cursor}}


@router.get('/{promptId}/versions/{promptVersionId}', response_model=Data[PromptVersionResponse])
def version_detail(promptId: ResourceId, promptVersionId: ResourceId, service: OwnedService, db: Db):
    get_prompt(db, service.id, promptId)
    version = get_version(db, service.id, promptVersionId)
    if version.prompt_id != promptId:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프롬프트 버전을 찾을 수 없습니다.')
    return {'data': version_data(version)}
