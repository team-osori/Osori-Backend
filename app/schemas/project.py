from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=50)]
    description: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator('name', 'description', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('description')
    @classmethod
    def nonblank_description(cls, value):
        if value is not None and not value:
            raise ValueError('설명은 공백만 입력할 수 없습니다.')
        return value


class ChatbotCreate(ProjectCreate):
    pass
