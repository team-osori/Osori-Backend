from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models.prompt import Prompt, PromptVersion


def get_prompt(db: Session, service_id: str, prompt_id: str) -> Prompt:
    prompt = db.scalar(select(Prompt).where(Prompt.id == prompt_id, Prompt.service_id == service_id))
    if prompt is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '프롬프트를 찾을 수 없습니다.')
    return prompt


def get_version(db: Session, service_id: str, version_id: str) -> PromptVersion:
    version = db.scalar(select(PromptVersion).join(Prompt).where(
        PromptVersion.id == version_id, Prompt.service_id == service_id))
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
