"""B-owned contract for frozen expectations supplied by question generation."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.evaluation import CriterionResult
from app.schemas.policy import Id

AnalysisCriterion = Literal['policy', 'questionResolution', 'communicationQuality', 'retrieval', 'grounding']
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ExpectedCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Id
    criterion: AnalysisCriterion
    requirement: RequiredText
    policyVersionId: Id
    sourceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000, pattern=r'\S')]
    kind: Literal['required', 'forbidden'] = 'required'
    category: Literal['policy', 'response', 'retrieval', 'grounding', 'communicationQuality'] = 'policy'
    ruleKeys: Annotated[list[Id], Field(max_length=100)] = []


IssueKind = Literal['condition_omitted', 'policy_contradiction', 'unsupported_claim',
                    'incomplete_guidance', 'non_answer', 'unclear_answer', 'other']


class IssueDetail(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: IssueKind
    answerQuote: RequiredText | None = None
    missingGuidance: Annotated[list[RequiredText], Field(max_length=20)] = []
    recommendation: RequiredText

    @model_validator(mode='after')
    def require_observation(self):
        if not self.answerQuote and not self.missingGuidance:
            raise ValueError('A finding requires an answer quote or omitted guidance')
        return self


class ConditionResult(CriterionResult):
    conditionId: Id
    issue: IssueDetail | None = None

    @model_validator(mode='after')
    def require_issue_for_failure(self):
        if (self.status == 'judged' and self.verdict == 'fail') != (self.issue is not None):
            raise ValueError('Only failed conditions require an issue and recommendation')
        return self


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
