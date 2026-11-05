from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.policy import Id

Criterion = Literal['retrieval', 'grounding', 'response', 'policy']
Verdict = Literal['PASS', 'FAIL', 'NOT_EVALUABLE']


class RetrievalDocument(BaseModel):
    """Source content returned by the company API, not a URL fetched by Osori."""
    model_config = ConfigDict(extra='forbid')
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    title: Annotated[str | None, Field(max_length=500)] = None
    source: Annotated[str | None, Field(max_length=2048)] = None


class Citation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    documentId: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=10000, pattern=r'\S')]


class Evidence(BaseModel):
    """Distinguish unavailable retrieval telemetry (None) from zero retrieved documents ([])."""
    model_config = ConfigDict(extra='forbid')
    documents: Annotated[list[RetrievalDocument] | None, Field(max_length=20)] = None
    citations: Annotated[list[Citation] | None, Field(max_length=50)] = None

    @model_validator(mode='after')
    def validate_sources(self):
        """Require stable unique source IDs and citations present verbatim in the returned documents."""
        if self.documents is not None:
            by_id = {doc.id: doc.text for doc in self.documents}
            if len(by_id) != len(self.documents) or any(not doc.text.strip() for doc in self.documents):
                raise ValueError('Invalid retrieval documents')
            if any(c.documentId not in by_id or c.quote not in by_id[c.documentId] for c in self.citations or []):
                raise ValueError('Invalid citation')
        elif self.citations:
            raise ValueError('Citations require documents')
        return self


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Verdict
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    evidence: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r'\S')]], Field(max_length=20)] = Field(default_factory=list)


class Judgement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    retrieval: CriterionResult
    grounding: CriterionResult
    response: CriterionResult
    policy: CriterionResult


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connectionRevision: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    testSetVersionId: Id
    aPromptVersionId: Id
    bPromptVersionId: Id


class EvaluationResponse(BaseModel):
    id: str
    serviceId: str
    status: Literal['QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL_FAILED', 'FAILED']
    totalQuestions: int
    completedQuestions: int
    failedQuestions: int
    judge: dict
    comparisonAssurance: dict
    createdAt: str
    finishedAt: str | None


class EvaluationItemSummary(BaseModel):
    id: str
    questionId: str
    question: str
    status: Literal['QUEUED', 'RUNNING', 'COMPLETED', 'FAILED']
    aCallStatus: Literal['NOT_RUN', 'SUCCEEDED', 'FAILED']
    bCallStatus: Literal['NOT_RUN', 'SUCCEEDED', 'FAILED']
    changes: dict[str, str]


class ExecutionError(BaseModel):
    code: str
    message: str


class EvaluationSide(BaseModel):
    callStatus: Literal['SUCCEEDED', 'FAILED']
    answer: str | None
    evidence: Evidence
    latencyMs: int
    attemptCount: int
    error: ExecutionError | None
    judgeError: ExecutionError | None
    criteria: Judgement


class EvaluationItemResponse(EvaluationItemSummary):
    input: dict
    a: EvaluationSide | None
    b: EvaluationSide | None


class EvaluationInputs(BaseModel):
    schemaVersion: int
    connection: dict
    prompts: dict
    testSet: dict
    criteria: dict
    judge: dict
    comparisonAssurance: dict


class CriterionCounts(BaseModel):
    passed: int = Field(alias='pass')
    fail: int
    notEvaluable: int
    callFailed: int
    notRun: int
    denominator: int
    passRate: float | None


class ComparisonCounts(BaseModel):
    IMPROVED: int
    WORSENED: int
    UNCHANGED_PASS: int
    UNCHANGED_FAIL: int
    NOT_COMPARABLE: int
    pairedDenominator: int
    aPairedPassRate: float | None
    bPairedPassRate: float | None


class CriterionSummary(BaseModel):
    a: CriterionCounts
    b: CriterionCounts
    comparison: ComparisonCounts


class CriteriaSummary(BaseModel):
    retrieval: CriterionSummary
    grounding: CriterionSummary
    response: CriterionSummary
    policy: CriterionSummary


class EvaluationSummary(BaseModel):
    evaluation: EvaluationResponse
    criteria: CriteriaSummary
    formula: str
    provisional: bool
