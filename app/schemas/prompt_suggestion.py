from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.prompt import Identifier


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SuggestionCreate(StrictModel):
    evaluationId: Identifier
    baseVersionId: Identifier
    itemIds: Annotated[list[Identifier], Field(min_length=1, max_length=20)]

    @field_validator('itemIds')
    @classmethod
    def unique_items(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('문항 ID가 중복되었습니다.')
        return sorted(value)


class SuggestionSelection(StrictModel):
    expectedRevision: Annotated[int, Field(strict=True, ge=1)]
    selectedSuggestionIds: Annotated[list[Identifier], Field(max_length=10)]


class GeneratedEdit(StrictModel):
    originalText: Annotated[str, Field(min_length=1, max_length=100_000)]
    replacementText: Annotated[str, Field(max_length=100_000)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    evidenceIds: Annotated[list[Identifier], Field(min_length=1, max_length=100)]


class GeneratedSuggestions(StrictModel):
    suggestions: Annotated[list[GeneratedEdit], Field(min_length=1, max_length=10)]


class SuggestionEdit(GeneratedEdit):
    id: str
    start: int
    end: int


class EffectEstimate(BaseModel):
    expectedImprovedQuestionCount: None = None
    targetedQuestionCount: int
    method: Literal['NOT_ESTIMABLE'] = 'NOT_ESTIMABLE'
    uncertainty: Literal['UNKNOWN'] = 'UNKNOWN'
    limitations: list[str]


class SuggestionError(BaseModel):
    code: str
    message: str


class SuggestionResponse(BaseModel):
    id: str
    evaluationId: str
    baseVersionId: str
    appliedPromptVersionId: str | None
    status: Literal['GENERATING', 'READY', 'FAILED', 'APPLIED']
    revision: int
    snapshot: dict
    suggestions: list[SuggestionEdit]
    selectedSuggestionIds: list[str]
    previewContent: str | None
    estimate: EffectEstimate
    error: SuggestionError | None
    createdAt: str
    updatedAt: str
    links: dict[str, str]


class SuggestionSummary(BaseModel):
    id: str
    evaluationId: str
    baseVersionId: str
    appliedPromptVersionId: str | None
    status: str
    revision: int
    createdAt: str


class SuggestionRerunLink(StrictModel):
    evaluationId: Identifier


class SuggestionRerunResponse(BaseModel):
    suggestionId: str
    baselineEvaluationId: str
    evaluationId: str
    promptVersionId: str
    comparisonUrl: str
