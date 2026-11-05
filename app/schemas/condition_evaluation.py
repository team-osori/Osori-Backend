"""B-owned contract for frozen expectations supplied by question generation."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.evaluation import CriterionResult
from app.schemas.policy import Id

AnalysisCriterion = Literal['policy', 'questionResolution', 'communicationQuality']
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ExpectedCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Id
    criterion: AnalysisCriterion
    requirement: RequiredText
    policyVersionId: Id
    sourceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000, pattern=r'\S')]


class ConditionResult(CriterionResult):
    conditionId: Id


class ScoredResult(CriterionResult):
    score: Annotated[int | None, Field(strict=True, ge=0, le=4)]

    @model_validator(mode='after')
    def validate_score(self):
        if self.status == 'judged':
            if self.score is None or (self.score >= 3) != (self.verdict == 'pass'):
                raise ValueError('A score of at least 3 is required to pass')
        elif self.score is not None:
            raise ValueError('Unavailable results cannot carry scores')
        return self


class ConditionJudgement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy: CriterionResult
    questionResolution: ScoredResult
    communicationQuality: ScoredResult
    conditionResults: Annotated[list[ConditionResult], Field(min_length=1, max_length=100)]
