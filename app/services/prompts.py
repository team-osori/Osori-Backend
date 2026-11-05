from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.concurrency import is_write_conflict
from app.services.auth import now
from app.models.prompt import Prompt, PromptVersion


def get_prompt(db: Session, service_id: str, prompt_id: str) -> Prompt:
    prompt = db.scalar(select(Prompt).where(Prompt.id == prompt_id, Prompt.service_id == service_id, Prompt.deleted.is_(False)))
    if prompt is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프롬프트를 찾을 수 없습니다.')
    return prompt


def get_version(db: Session, service_id: str, version_id: str, *, allow_deleted=False, lock=False) -> PromptVersion:
    statement = select(PromptVersion).join(Prompt).where(PromptVersion.id == version_id, Prompt.service_id == service_id)
    if not allow_deleted:
        statement = statement.where(Prompt.deleted.is_(False))
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    version = db.scalar(statement)
    if version is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프롬프트 버전을 찾을 수 없습니다.')
    return version


def select_ab_versions(db: Session, service_id: str, a_version_id: str,
                       b_version_id: str) -> tuple[PromptVersion, PromptVersion]:
    """Evaluation integration: authorize both versions and enforce one prompt, two versions."""
    a = get_version(db, service_id, a_version_id)
    b = get_version(db, service_id, b_version_id)
    if a.id == b.id:
        raise ApiError(400, 'SAME_PROMPT_VERSION', '서로 다른 프롬프트 버전을 선택해 주세요.')
    if a.prompt_id != b.prompt_id:
        raise ApiError(409, 'PROMPT_SCOPE_MISMATCH', '같은 프롬프트의 버전을 선택해 주세요.')
    return a, b


def new_version(body, prompt_id, number, version_id=None):
    return PromptVersion(id=version_id or str(uuid4()), prompt_id=prompt_id, version=number,
        content=body.content, label=body.label, change_note=body.changeNote,
        external_version_ref=body.externalVersionRef, created_at=now())


def save_version(db, service_id, prompt, body):
    """Shared guarded immutable-version insert; caller commits related records atomically."""
    if prompt.current_version_id != body.baseVersionId:
        raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '최신 버전을 기준으로 저장해 주세요.')
    previous = get_version(db, service_id, body.baseVersionId)
    if previous.content == body.content and previous.external_version_ref == body.externalVersionRef:
        raise ApiError(409, 'NO_CHANGES', '원문 또는 외부 버전 참조를 변경해 주세요.')
    version = new_version(body, prompt.id, previous.version + 1)
    try:
        changed = db.execute(update(Prompt).where(Prompt.id == prompt.id,
            Prompt.current_version_id == body.baseVersionId, Prompt.deleted.is_(False)).values(
            current_version_id=version.id, revision=Prompt.revision + 1, updated_at=now()))
    except OperationalError as exc:
        db.rollback()
        if is_write_conflict(exc):
            raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '다른 수정본이 먼저 저장되었습니다.') from None
        raise
    if changed.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'PROMPT_VERSION_CONFLICT', '다른 수정본이 먼저 저장되었습니다.')
    db.add(version)
    db.flush()
    return version
