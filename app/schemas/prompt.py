from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]


class PromptContent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: Annotated[str, Field(min_length=1, max_length=100_000)]
    label: Annotated[str | None, Field(max_length=100)] = None
    changeNote: Annotated[str | None, Field(max_length=2000)] = None
    externalVersionRef: Annotated[str | None, Field(max_length=200)] = None

    @field_validator('content')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('프롬프트 원문이 필요합니다.')
        return value  # Whitespace is part of the prompt; never trim it.


class PromptCreate(PromptContent):
    name: Annotated[str, Field(min_length=1, max_length=100)] = '기본 프롬프트'

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class PromptVersionCreate(PromptContent):
    baseVersionId: Identifier


class PromptVersionSummary(BaseModel):
    id: str
    promptId: str
    version: int
    label: str | None
    changeNote: str | None
    externalVersionRef: str | None
    createdAt: str


class PromptVersionResponse(PromptVersionSummary):
    content: str


class PromptSummary(BaseModel):
    id: str
    name: str
    currentVersionId: str
    version: int
    revision: int
    createdAt: str
    updatedAt: str


class PromptResponse(BaseModel):
    id: str
    serviceId: str
    name: str
    revision: int
    currentVersionId: str
    currentVersion: PromptVersionResponse
    createdAt: str
    updatedAt: str
