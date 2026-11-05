import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.policy_document import DocumentSource

Text = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Id = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]
RevisionNumber = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @field_validator('*')
    @classmethod
    def not_blank(cls, value):
        """Preserve original text but reject whitespace-only strings."""
        if isinstance(value, str) and not value.strip():
            raise ValueError('공백만 입력할 수 없습니다.')
        return value


class Rule(Input):
    key: Id = Field(description='정책 버전 안에서 중복 불가. 영문·숫자·밑줄·하이픈 1~100자')
    description: Text
    sourceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000)] = Field(
        description='content에 그대로 포함된 연속 원문. 공백·줄바꿈·문장부호까지 일치해야 합니다.')


class PolicyBody(Input):
    sourceDocument: DocumentSource | None = None

    content: Annotated[str, StringConstraints(min_length=1, max_length=100000)]
    rules: Annotated[list[Rule], Field(min_length=1, max_length=100,
        description='클라이언트가 검토한 규칙 1~100개가 필수입니다.')]

    @model_validator(mode='after')
    def source_matches_content(self):
        source = self.sourceDocument
        if source is None:
            return self
        if hashlib.sha256(self.content.encode()).hexdigest() != source.contentHash:
            raise ValueError('원문을 수정했다면 sourceDocument를 제거하고 저장하세요.')
        end = 0
        for chunk in source.chunks:
            if chunk.endOffset > len(self.content) or self.content[end:chunk.startOffset].strip():
                raise ValueError('원문과 청크 범위가 일치하지 않습니다.')
            end = chunk.endOffset
        if self.content[end:].strip():
            raise ValueError('원문과 청크 범위가 일치하지 않습니다.')
        return self

    @field_validator('rules')
    @classmethod
    def evidence_matches(cls, rules, info: ValidationInfo):
        """Require unique rule keys and verbatim supporting passages in the original text."""
        if rules is None:
            return rules
        seen = set()
        for index, rule in enumerate(rules):
            if rule.key in seen:
                raise PydanticCustomError('policy_rule_key_duplicate', '규칙 key는 중복될 수 없습니다.', {'rule_index': index})
            seen.add(rule.key)
            if 'content' in info.data and rule.sourceQuote not in info.data['content']:
                raise PydanticCustomError('policy_rule_source_mismatch', '규칙의 근거는 정책 원문에 포함되어야 합니다.', {'rule_index': index})
        return rules


class PolicyCreate(PolicyBody):
    name: Name
    rules: Annotated[list[Rule] | None, Field(min_length=1, max_length=100,
        description='생략하면 모델이 규칙을 추출해 미저장 draft를 반환합니다. 명시하면 검증 후 정책을 저장합니다.')] = None


class PolicyDraft(PolicyBody):
    name: Name
    status: Literal['draft'] = 'draft'
    persisted: Literal[False] = False


class ImportFailure(BaseModel):
    code: str
    message: str
    httpStatus: int
    retryAfter: str | None = None


class ImportSuccess(BaseModel):
    filename: str
    status: Literal['draft'] = 'draft'
    draft: PolicyDraft


class ImportFailed(BaseModel):
    filename: str
    status: Literal['failed'] = 'failed'
    error: ImportFailure


class PolicyImports(BaseModel):
    items: list[ImportSuccess | ImportFailed]


class PolicyVersionCreate(PolicyBody):
    expectedRevision: RevisionNumber


class Revision(Input):
    expectedRevision: RevisionNumber


class Rename(Revision):
    name: Name


class TestSetCreate(Input):
    name: Name
    policyVersionIds: Annotated[list[Id], Field(min_length=1, max_length=20)]

    @field_validator('policyVersionIds')
    @classmethod
    def unique_versions(cls, value):
        """Reject duplicate policy references rather than silently changing user input."""
        if len(value) != len(set(value)):
            raise ValueError('정책 버전이 중복되었습니다.')
        return value


class NewDraft(Revision):
    sourceVersionId: Id
    policyVersionIds: Annotated[list[Id] | None, Field(min_length=1, max_length=20)] = None

    @field_validator('policyVersionIds')
    @classmethod
    def unique_versions(cls, value):
        """Allow selecting newer sources explicitly without copying outdated questions."""
        if value is not None and len(value) != len(set(value)):
            raise ValueError('정책 버전이 중복되었습니다.')
        return value


class ExpectedCondition(Input):
    id: Id | None = None
    description: Text
    kind: Literal['required', 'forbidden'] = 'required'
    category: Literal['retrieval', 'grounding', 'response', 'policy'] = 'policy'
    policyVersionId: Id
    ruleKeys: Annotated[list[Id], Field(min_length=1, max_length=100)]
    evidenceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000)]


QuestionCategory = Literal['normal', 'ambiguous', 'missing_document', 'policy_trap', 'exception', 'boundary']


class QuestionBody(Input):
    question: Text
    expectedBehavior: Text | None = None
    modelAnswer: Text | None = None
    forbiddenBehaviors: Annotated[list[Text], Field(max_length=20)] = []
    policyVersionId: Id | None = None
    ruleKeys: Annotated[list[Id], Field(max_length=100)] = []
    evidenceQuote: Annotated[str | None, StringConstraints(min_length=1, max_length=10000)] = None
    category: QuestionCategory = 'normal'
    expectedConditions: Annotated[list[ExpectedCondition] | None, Field(min_length=1, max_length=30)] = None

    @model_validator(mode='after')
    def answer_and_evidence(self):
        if self.expectedBehavior is None:
            if self.modelAnswer is None:
                raise ValueError('기대 행동 또는 모범 답안이 필요합니다.')
            self.expectedBehavior = self.modelAnswer
        linked = (self.policyVersionId is not None, bool(self.ruleKeys), self.evidenceQuote is not None)
        if any(linked) and not all(linked):
            raise ValueError('정책 버전·규칙·근거 원문을 함께 연결하세요.')
        if not all(linked) and self.expectedConditions is not None:
            raise ValueError('조건을 작성하기 전에 문항 근거를 연결하세요.')
        return self

    @field_validator('forbiddenBehaviors')
    @classmethod
    def no_blank_behaviors(cls, values):
        """Reject blank forbidden actions within nested arrays."""
        if any(not value.strip() for value in values):
            raise ValueError('금지 행동은 공백일 수 없습니다.')
        return values


class QuestionWrite(QuestionBody, Revision):
    changeNote: Annotated[str | None, StringConstraints(max_length=2000)] = None


class Review(Revision):
    status: Literal['approved', 'rejected']
    note: Annotated[str | None, StringConstraints(max_length=2000)] = None


class Generate(Revision):
    count: Annotated[int, Field(strict=True, ge=1, le=50)] = 10
    categories: Annotated[list[QuestionCategory] | None, Field(min_length=1, max_length=6)] = None
    categoryCounts: dict[QuestionCategory, Annotated[int, Field(strict=True, ge=0, le=50)]] | None = None

    @model_validator(mode='after')
    def category_coverage(self):
        if self.categoryCounts is not None:
            total = sum(self.categoryCounts.values())
            if self.categories is not None or not 1 <= total <= 50:
                raise ValueError('categoryCounts 합계는 1~50이며 categories와 함께 보낼 수 없습니다.')
            if 'count' in self.model_fields_set and self.count != total:
                raise ValueError('count는 유형별 개수의 합계와 같아야 합니다.')
            self.count = total
        if self.categories is not None:
            if len(set(self.categories)) != len(self.categories) or self.count < len(self.categories):
                raise ValueError('유형은 중복 없이 선택하고 유형 수 이상의 문항을 요청하세요.')
        return self


class GeneratedQuestions(Input):
    questions: Annotated[list[QuestionBody], Field(min_length=1, max_length=50)]


class PolicyOut(BaseModel):
    id: str
    projectId: str
    name: str
    revision: int
    createdAt: str


class PolicyVersionOut(BaseModel):
    sourceDocument: DocumentSource | None = None
    id: str
    policyId: str
    version: int
    content: str
    rules: list[Rule]
    createdAt: str


class PolicyDetail(BaseModel):
    policy: PolicyOut
    version: PolicyVersionOut


class TestSetOut(BaseModel):
    id: str
    projectId: str
    name: str
    revision: int
    createdAt: str


class QuestionOut(QuestionBody):
    id: str
    reviewStatus: Literal['pending', 'approved', 'rejected']
    reviewedBy: str | None
    reviewNote: str | None
    reviewedAt: str | None = None


class QuestionHistoryOut(BaseModel):
    id: str
    questionId: str
    versionRevision: int
    action: Literal['created', 'generated', 'copied', 'edited', 'reviewed', 'deleted']
    actorId: str
    note: str | None
    before: dict | None
    after: dict | None
    createdAt: str


class TestSetVersionSummary(BaseModel):
    id: str
    testSetId: str
    version: int
    revision: int
    status: Literal['draft', 'confirmed']
    policyVersionIds: list[str]
    confirmedBy: str | None
    confirmedAt: str | None
    createdAt: str


class TestSetVersionOut(TestSetVersionSummary):
    questions: list[QuestionOut]
    questionCount: int = 0
    approvedCount: int = 0

    @model_validator(mode='before')
    @classmethod
    def legacy_cached_counts(cls, value):
        # Generation replays saved before #27 do not contain the additive counters.
        if isinstance(value, dict) and 'questions' in value:
            rows = value['questions']
            value = {**value}
            value.setdefault('questionCount', len(rows))
            value.setdefault('approvedCount', sum(q.get('reviewStatus') == 'approved' and q.get('reviewedBy') is not None for q in rows))
        return value


class TestSetDetail(BaseModel):
    testSet: TestSetOut
    version: TestSetVersionOut


class EvaluationSnapshot(BaseModel):
    schemaVersion: int
    projectId: str
    testSetId: str
    testSetVersionId: str
    version: int
    confirmedBy: str
    confirmedAt: str
    policies: list[PolicyVersionOut]
    questions: list[QuestionOut]
