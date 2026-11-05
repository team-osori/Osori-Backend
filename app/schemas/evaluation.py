from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.policy import Id

CauseArea = Literal['RETRIEVAL', 'PROMPT', 'MODEL']
AnalysisStatus = Literal['HYPOTHESIS_AVAILABLE', 'UNDETERMINED', 'UNAVAILABLE', 'NOT_APPLICABLE']
ScreenStatus = Literal['CORRECT', 'INCORRECT', 'PENDING', 'REMAINING', 'EXECUTION_ERROR', 'JUDGMENT_ERROR', 'REVIEW_REQUIRED']

Criterion = Literal['retrieval', 'grounding', 'response', 'policy', 'questionResolution', 'communicationQuality']


class RetrievalDocument(BaseModel):
    """Source content returned by the company API, not a URL fetched by Osori."""
    # 기업 API는 점수 · 페이지 같은 메타데이터를 함께 주는 경우가 많다(예: 팀 RAG의 score · page).
    # 알 수 없는 필드는 저장하지 않고 버리며, 아래 필드의 검증은 그대로 한다.
    model_config = ConfigDict(extra='ignore')
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    title: Annotated[str | None, Field(max_length=500)] = None
    source: Annotated[str | None, Field(max_length=2048)] = None
    chunkIndex: Annotated[int | None, Field(strict=True, ge=0)] = Field(default=None,
        description='기업 API가 반환한 청크 번호. 0/1 기반 번호를 변환하지 않으며, 미제공 시 null.')


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


class ExecutionError(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    evidence_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class EvidenceSource(BaseModel):
    evidence_id: str
    kind: Literal['answer', 'policy', 'retrieval']
    source_id: str | None


class ReasonMark(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r'\S')] = Field(
        description='hypothesis.reason에 공백·문장부호까지 그대로 포함되는 표시 문자열.')
    kind: Literal['policy', 'retrieval', 'prompt']
    evidenceRef: Annotated[str | None, StringConstraints(min_length=1, max_length=100)] = Field(default=None,
        description='policy/retrieval은 같은 side의 evidenceCatalog ID가 필수. prompt는 null.')


class CauseHypothesis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    area: CauseArea
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    evidenceRefs: Annotated[list[EvidenceReference], Field(min_length=1, max_length=20)]
    promptQuote: Annotated[str | None, StringConstraints(min_length=1, max_length=10000, pattern=r'\S')] = Field(
        default=None, description='PROMPT 가설의 평가 당시 promptContext.content 원문 인용. 원문 미확인 시 null.')
    reasonMarks: Annotated[list[ReasonMark], Field(max_length=20)] = []


IssueKind = Literal['condition_omitted', 'policy_contradiction', 'unsupported_claim',
                    'incomplete_guidance', 'non_answer', 'unclear_answer', 'other']


class AnalysisIssueDetail(BaseModel):
    kind: IssueKind
    answerQuote: str | None = None
    missingGuidance: list[str] = []
    recommendation: str | None = None
    hypothesis: CauseHypothesis | None = None


class ConditionJudgmentOutput(BaseModel):
    """Includes human review overrides, which need not contain automatic evidence refs."""
    question_id: str
    conditionId: str
    status: Literal['judged', 'not_evaluable', 'error']
    verdict: Literal['pass', 'fail'] | None
    reason: str
    evidence_refs: list[EvidenceReference]
    error: ExecutionError | None = None
    issue: AnalysisIssueDetail | None = None
    humanReview: dict | None = None


class PolicyEvidence(BaseModel):
    policyVersionId: str
    quote: str
    policyTitle: str | None = None
    fileName: str | None = None
    chunkIndex: int | None = Field(default=None,
        description='원문 인용이 하나의 청크로 특정될 때의 1 기반 번호. 미제공·다중 청크·모호한 위치는 null.')


class AnalysisCondition(BaseModel):
    conditionId: str
    criterion: Literal['policy', 'questionResolution', 'communicationQuality', 'retrieval', 'grounding']
    kind: Literal['required', 'forbidden']
    category: Literal['policy', 'response', 'retrieval', 'grounding', 'communicationQuality']
    ruleKeys: list[str]
    requirement: str
    policyEvidence: PolicyEvidence
    judgment: ConditionJudgmentOutput | None
    automaticJudgment: ConditionJudgmentOutput | None


class AnalysisIssue(AnalysisCondition):
    kind: IssueKind
    reason: str
    answerQuote: str | None = None
    missingGuidance: list[str] = []
    recommendation: str | None = None
    hypothesis: CauseHypothesis | None = None


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question_id: Annotated[str, StringConstraints(min_length=1, max_length=36)]
    status: Literal['judged', 'not_evaluable', 'error']
    verdict: Literal['pass', 'fail'] | None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    evidence_refs: Annotated[list[EvidenceReference], Field(max_length=20)]
    error: ExecutionError | None

    @model_validator(mode='after')
    def validate_outcome(self):
        """A missing judgement or execution error cannot masquerade as a pass/fail verdict."""
        ids = [ref.evidence_id for ref in self.evidence_refs]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate evidence references')
        if self.status == 'judged':
            if self.verdict is None or not self.evidence_refs or self.error is not None:
                raise ValueError('Judged results require a verdict and evidence without an error')
        elif self.status == 'not_evaluable':
            if self.verdict is not None or self.error is not None:
                raise ValueError('An unavailable judgement cannot have a verdict or error')
        elif self.verdict is not None or self.error is None or self.evidence_refs:
            raise ValueError('Errors require error details and no verdict or evidence')
        return self


class Judgement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    retrieval: CriterionResult
    grounding: CriterionResult
    response: CriterionResult
    policy: CriterionResult


class ScoredCriterionOutput(CriterionResult):
    score: Annotated[int | None, Field(ge=0, le=4)]


class ConditionCriteriaOutput(BaseModel):
    policy: CriterionResult
    questionResolution: ScoredCriterionOutput
    communicationQuality: ScoredCriterionOutput


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connectionRevision: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    testSetVersionId: Id
    mode: Literal['SINGLE', 'COMPARE'] | None = None
    promptVersionId: Id | None = None
    aPromptVersionId: Id | None = None
    bPromptVersionId: Id | None = None
    sourceEvaluationId: Id | None = None

    @model_validator(mode='after')
    def validate_targets(self):
        if self.resolved_mode == 'COMPARE':
            if not self.aPromptVersionId or not self.bPromptVersionId or self.promptVersionId or self.sourceEvaluationId:
                raise ValueError('비교에는 A/B 프롬프트 두 개를 지정하세요.')
        elif self.aPromptVersionId or self.bPromptVersionId:
            raise ValueError('단일 평가에는 promptVersionId만 선택할 수 있습니다.')
        return self

    @property
    def resolved_mode(self):
        if self.mode:
            return self.mode
        return 'COMPARE' if self.aPromptVersionId or self.bPromptVersionId else 'SINGLE'


class EvaluationRerun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connectionRevision: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    promptVersionId: Id | None = None


class EvaluationAction(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EvaluationResume(EvaluationAction):
    connectionRevision: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]


class AnalysisCapability(BaseModel):
    supported: bool
    reason: str


class AnalysisCapabilities(BaseModel):
    causeClassification: AnalysisCapability
    causeHypotheses: AnalysisCapability
    promptSuggestions: AnalysisCapability
    promptDiff: AnalysisCapability
    interRaterAgreement: AnalysisCapability
    unstableQuestions: AnalysisCapability
    fixableQuestionCount: AnalysisCapability


class EvaluationResponse(BaseModel):
    id: str
    serviceId: str
    mode: Literal['SINGLE', 'COMPARE'] = 'COMPARE'
    sourceEvaluationId: str | None = None
    status: Literal['QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED']
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
    questionNumber: int | None = None
    status: Literal['QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'NOT_RUN', 'INTERRUPTED']
    aCallStatus: Literal['NOT_RUN', 'RUNNING', 'SUCCEEDED', 'FAILED', 'INTERRUPTED']
    bCallStatus: Literal['NOT_APPLICABLE', 'NOT_RUN', 'RUNNING', 'SUCCEEDED', 'FAILED', 'INTERRUPTED']
    changes: dict[str, str]
    resultStatus: str | None = None
    needsReview: bool = False
    screenStatus: ScreenStatus
    analysisStatus: AnalysisStatus
    activeHypothesisAreas: list[CauseArea]


class EvaluationSide(BaseModel):
    callStatus: Literal['NOT_RUN', 'RUNNING', 'SUCCEEDED', 'FAILED', 'INTERRUPTED']
    answer: str | None
    evidence: Evidence
    evidenceCatalog: list[EvidenceSource]
    latencyMs: int | None
    attemptCount: int | None
    error: ExecutionError | None
    judgeError: ExecutionError | None
    criteria: ConditionCriteriaOutput | Judgement
    conditionResults: list[ConditionJudgmentOutput] = []


class PromptContext(BaseModel):
    source: Literal['SELECTED_VERSION', 'CONFIGURED_TEMPLATE', 'UNAVAILABLE']
    available: bool
    promptVersionId: str | None
    promptId: str | None
    version: int | None
    content: str | None
    pointer: str | None
    connectionRevision: int | None
    applicationVerified: bool


class AnalysisHypothesis(CauseHypothesis):
    conditionId: str


class ItemAnalysis(BaseModel):
    status: str
    screenStatus: ScreenStatus
    derivedScreenStatus: ScreenStatus
    needsReview: bool
    hasJudgmentError: bool
    hasUnevaluableCriteria: bool
    conditions: list[AnalysisCondition]
    issues: list[AnalysisIssue]
    humanReviewApplied: bool
    causeStatus: Literal['HYPOTHESIS', 'UNDETERMINED']
    confirmedCause: None
    facts: dict
    hypotheses: list[AnalysisHypothesis]
    limitations: list[str]
    activeHypotheses: list[AnalysisHypothesis]
    activeHypothesisAreas: list[CauseArea]
    analysisStatus: AnalysisStatus
    hypothesesBasis: Literal['AUTOMATIC']
    activeHypothesesBasis: Literal['LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC']
    promptContext: PromptContext
    answerReview: dict | None
    reviewConflict: bool


class EvaluationItemResponse(EvaluationItemSummary):
    previousItemId: str | None = None
    nextItemId: str | None = None
    input: dict
    a: EvaluationSide | None
    b: EvaluationSide | None
    analysis: dict[str, ItemAnalysis] = {}
    reviewRevision: int = 0
    latestReviews: list[dict] = []


class EvaluationInputs(BaseModel):
    schemaVersion: int
    connection: dict
    prompts: dict
    testSet: dict
    criteria: dict
    judge: dict
    comparisonAssurance: dict
    mode: Literal['SINGLE', 'COMPARE'] = 'COMPARE'
    useConfiguredPrompt: bool = False
    sourceEvaluationId: str | None = None


class ScoreSummary(BaseModel):
    average: float | None
    denominator: int
    minimum: int = 0
    maximum: int = 4
    passThreshold: int
    basis: Literal['AUTOMATIC'] = 'AUTOMATIC'


class CriterionCounts(BaseModel):
    scoreSummary: ScoreSummary | None = None
    passed: int = Field(alias='pass')
    fail: int
    notEvaluable: int
    judgeFailed: int
    callFailed: int
    callInterrupted: int
    inProgress: int
    notRun: int
    denominator: int
    passRate: float | None = Field(description='통과율 백분율(0~100). 판정된 문항이 없으면 null입니다.')


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
    b: CriterionCounts | None
    comparison: ComparisonCounts | None


class CriteriaSummary(BaseModel):
    retrieval: CriterionSummary
    grounding: CriterionSummary
    response: CriterionSummary
    policy: CriterionSummary


class ConditionCriteriaSummary(BaseModel):
    policy: CriterionSummary
    questionResolution: CriterionSummary
    communicationQuality: CriterionSummary


class ScreenCounts(BaseModel):
    correct: int
    incorrect: int
    pending: int
    remaining: int
    executionError: int
    judgmentError: int
    reviewRequired: int
    total: int


class AnalysisVariantSummary(BaseModel):
    statuses: dict[str, int]
    needsReviewCount: int
    issueTypes: dict[str, int]
    screenCounts: ScreenCounts
    causeQuestionCounts: dict[CauseArea, int]
    analysisStatusCounts: dict[AnalysisStatus, int]


class AnalysisSummary(BaseModel):
    variants: dict[str, AnalysisVariantSummary]
    issueCountsIncludeMultipleIssuesPerQuestion: bool
    causeCountsIncludeMultipleAreasPerQuestion: bool
    causeCountUnit: Literal['QUESTION']
    basis: Literal['LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC']
    criterionPassRatesBasis: Literal['AUTOMATIC']


class EvaluationSummary(BaseModel):
    evaluation: EvaluationResponse
    criteria: ConditionCriteriaSummary | CriteriaSummary
    formula: str
    provisional: bool
    analysis: AnalysisSummary


class ConditionComparison(BaseModel):
    conditionId: str
    criterion: Literal['policy', 'questionResolution', 'communicationQuality', 'retrieval', 'grounding']
    change: Literal['RESOLVED', 'REMAINING', 'NEW', 'UNCHANGED_PASS', 'NOT_COMPARABLE']
    before: Literal['pass', 'fail'] | None
    after: Literal['pass', 'fail'] | None


class ComparisonItem(BaseModel):
    questionId: str
    baselineItemId: str | None
    targetItemId: str | None
    conditions: list[ConditionComparison]
    baselineReviewRevision: int | None
    targetReviewRevision: int | None


class ConditionComparisonCounts(BaseModel):
    RESOLVED: int
    REMAINING: int
    NEW: int
    UNCHANGED_PASS: int
    NOT_COMPARABLE: int


class EvaluationComparison(BaseModel):
    baselineEvaluationId: str
    targetEvaluationId: str
    outcome: Literal['MIXED', 'REGRESSED', 'INCOMPLETE', 'IMPROVED', 'NO_IMPROVEMENT']
    counts: ConditionComparisonCounts
    countUnit: Literal['CONDITION']
    hasNewIssues: bool
    hasIncompleteComparison: bool
    basis: Literal['LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC']
    externalEnvironmentControlled: Literal[False]
    items: list[ComparisonItem]
    nextCursor: str | None
