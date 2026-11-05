from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

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
    key: Id
    description: Text
    sourceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000)]


class PolicyBody(Input):
    content: Annotated[str, StringConstraints(min_length=1, max_length=100000)]
    rules: Annotated[list[Rule], Field(min_length=1, max_length=100)]

    @model_validator(mode='after')
    def evidence_matches(self):
        """Require unique rule keys and verbatim supporting passages in the original text."""
        if len({rule.key for rule in self.rules}) != len(self.rules):
            raise ValueError('규칙 key는 중복될 수 없습니다.')
        if any(rule.sourceQuote not in self.content for rule in self.rules):
            raise ValueError('규칙의 근거는 정책 원문에 포함되어야 합니다.')
        return self


class PolicyCreate(PolicyBody):
    name: Name


class PolicyVersionCreate(PolicyBody):
    expectedRevision: RevisionNumber


class Revision(Input):
    expectedRevision: RevisionNumber


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


class QuestionBody(Input):
    question: Text
    expectedBehavior: Text
    forbiddenBehaviors: Annotated[list[Text], Field(max_length=20)] = []
    policyVersionId: Id
    ruleKeys: Annotated[list[Id], Field(min_length=1, max_length=100)]
    evidenceQuote: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    category: Literal['normal', 'exception', 'ambiguous'] = 'normal'

    @field_validator('forbiddenBehaviors')
    @classmethod
    def no_blank_behaviors(cls, values):
        """Reject blank forbidden actions within nested arrays."""
        if any(not value.strip() for value in values):
            raise ValueError('금지 행동은 공백일 수 없습니다.')
        return values


class QuestionWrite(QuestionBody, Revision):
    pass


class Review(Revision):
    status: Literal['approved', 'rejected']
    note: Annotated[str | None, StringConstraints(max_length=2000)] = None


class Generate(Revision):
    count: Annotated[int, Field(ge=1, le=50)] = 10


class GeneratedQuestions(Input):
    questions: Annotated[list[QuestionBody], Field(min_length=1, max_length=50)]


class PolicyOut(BaseModel):
    id: str
    projectId: str
    name: str
    revision: int
    createdAt: str


class PolicyVersionOut(BaseModel):
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
