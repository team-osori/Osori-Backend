from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=50)]
    description: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator('name', 'description', mode='before')
    @classmethod
    def trim(cls, value):
        """Trim display text while leaving non-string values for type validation."""
        return value.strip() if isinstance(value, str) else value

    @field_validator('description')
    @classmethod
    def nonblank_description(cls, value):
        """Permit absent descriptions but reject an explicitly blank description."""
        if value is not None and not value:
            raise ValueError('설명은 공백만 입력할 수 없습니다.')
        return value


class ChatbotCreate(ProjectCreate):
    pass


class ProgressTestSet(BaseModel):
    versionId: str
    label: str
    status: Literal['draft', 'confirmed']
    questionCount: int


class ProgressEvaluation(BaseModel):
    id: str
    status: Literal['QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL_FAILED', 'FAILED']
    completedQuestions: int
    totalQuestions: int
    reviewedQuestions: int = Field(description='모든 평가 대상의 모든 기대 조건에 최신 사람 pass/fail 판정이 있는 문항 수')


class ProjectProgress(BaseModel):
    stage: Literal[1, 2, 3] = 1
    unlockedStage: Literal[1, 2, 3] = 1
    status: Literal['SETUP', 'RUNNING', 'DONE'] = 'SETUP'
    testSet: ProgressTestSet | None = None
    evaluation: ProgressEvaluation | None = None
    lastEvaluatedAt: str | None = None
    promptVersion: int | None = Field(default=None,
        description='최신 평가의 A측 명시적 프롬프트 버전. 기업 설정 프롬프트 사용 또는 평가 없음은 null')
