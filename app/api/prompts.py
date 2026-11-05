from uuid import uuid4

from fastapi import APIRouter, Response
from sqlalchemy import select, update

from app.api.dependencies import Db, IfMatch, OwnedService, ResourceId
from app.api.projects import iso
from app.core.errors import ApiError
from app.core.pagination import Cursor, Limit, page_query
from app.models.prompt import Prompt, PromptVersion
from app.schemas.prompt import (PromptCreate, PromptResponse, PromptSummary,
    PromptVersionCreate, PromptVersionResponse, PromptVersionSummary)
from app.schemas.policy import Rename
from app.schemas.responses import Data, ERROR_RESPONSES, Page
from app.services.auth import now
from app.services.prompts import get_prompt, get_version, new_version, save_version
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
    rows, next_cursor = page_query(db, select(Prompt).where(Prompt.service_id == service.id, Prompt.deleted.is_(False)),
                                  Prompt, f'prompts:{service.id}', limit, cursor)
    version_numbers = dict(db.execute(select(PromptVersion.id, PromptVersion.version).where(PromptVersion.id.in_([p.current_version_id for p in rows]))).all())
    return {'data': {'items': [{'id': p.id, 'name': p.name, 'currentVersionId': p.current_version_id,
        'version': version_numbers[p.current_version_id], 'revision': p.revision, 'createdAt': iso(p.created_at),
        'updatedAt': iso(p.updated_at)} for p in rows], 'nextCursor': next_cursor}}


@router.get('/{promptId}', response_model=Data[PromptResponse])
def prompt_detail(promptId: ResourceId, service: OwnedService, db: Db):
    prompt = get_prompt(db, service.id, promptId)
    return {'data': prompt_data(prompt, get_version(db, service.id, prompt.current_version_id))}


@router.post('/{promptId}/versions', status_code=201, response_model=Data[PromptVersionResponse])
def create_version(promptId: ResourceId, body: PromptVersionCreate, service: OwnedService,
                   db: Db, response: Response):
    prompt = get_prompt(db, service.id, promptId)
    if body.suggestionId:
        from app.services.prompt_suggestions import apply_suggestions
        version = apply_suggestions(db, service.id, prompt, body)
    else:
        version = save_version(db, service.id, prompt, body)
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


@router.patch('/{promptId}', response_model=Data[PromptResponse])
def rename_prompt(promptId: ResourceId, body: Rename, service: OwnedService, db: Db):
    row = get_prompt(db, service.id, promptId)
    changed = db.execute(update(Prompt).where(Prompt.id == row.id, Prompt.deleted.is_(False),
        Prompt.revision == body.expectedRevision).values(name=body.name, revision=Prompt.revision + 1, updated_at=now()))
    if changed.rowcount != 1:
        raise ApiError(409, 'REVISION_CONFLICT', '최신 revision을 확인하세요.')
    db.commit()
    db.refresh(row)
    return {'data': prompt_data(row, get_version(db, service.id, row.current_version_id))}


@router.delete('/{promptId}', status_code=204)
def delete_prompt(promptId: ResourceId, service: OwnedService, db: Db, if_match: IfMatch = None):
    row = get_prompt(db, service.id, promptId)
    if if_match is None:
        raise ApiError(428, 'PRECONDITION_REQUIRED', 'If-Match 헤더가 필요합니다.')
    if if_match != f'"{row.revision}"':
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인하세요.')
    changed = db.execute(update(Prompt).where(Prompt.id == row.id, Prompt.deleted.is_(False),
        Prompt.revision == row.revision).values(deleted=True, revision=Prompt.revision + 1, updated_at=now()))
    if changed.rowcount != 1:
        raise ApiError(412, 'PRECONDITION_FAILED', '최신 revision을 확인하세요.')
    db.commit()
    return Response(status_code=204)
