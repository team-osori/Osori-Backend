import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.errors import ApiError
from app.schemas.prompt import Identifier
from app.services.json_pointer import read_pointer, tokens

RESERVED_HEADERS = {'host', 'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
    'content-length', 'transfer-encoding', 'connection', 'upgrade', 'te', 'trailer', 'expect',
    'accept-encoding', 'proxy-connection', 'forwarded', 'x-forwarded-for', 'x-forwarded-host'}


def header_name(value):
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
        raise ValueError('잘못된 헤더 이름입니다.')
    return value


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['NONE', 'BEARER', 'HEADER']
    headerName: Annotated[str | None, Field(max_length=100)] = None

    @model_validator(mode='after')
    def validate_header(self):
        if self.type == 'HEADER':
            if not self.headerName or header_name(self.headerName).lower() in RESERVED_HEADERS | {'content-type', 'accept'}:
                raise ValueError('사용할 수 없는 인증 헤더입니다.')
        elif self.headerName is not None:
            raise ValueError('HEADER 방식에서만 headerName을 사용합니다.')
        return self


class ConnectionPut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expectedRevision: Annotated[int, Field(ge=0, strict=True)]
    executionMode: Literal['PROMPT_BODY', 'CONNECTION_ONLY']
    method: Literal['POST']
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    headers: dict[str, str]
    requestTemplate: dict
    questionPointer: Annotated[str, Field(min_length=1, max_length=500)]
    answerPointer: Annotated[str, Field(min_length=1, max_length=500)]
    promptPointer: Annotated[str | None, Field(max_length=500)] = None
    retrievalPointer: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    citationsPointer: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    auth: AuthConfig
    timeoutMs: Annotated[int, Field(ge=100, le=60_000, strict=True)]
    maxRetries: Annotated[int, Field(ge=0, le=2, strict=True)]
    retrySafe: bool
    llmKeyHeaderName: Annotated[str | None, Field(max_length=100, description=(
        '평가 대상 서비스가 답변 생성에 쓰는 AI API 키를 실어 보낼 헤더 이름(예: X-OpenAI-Key). '
        '키 값은 PUT /connection/llm-secret으로 따로 등록합니다. 생략하면 보내지 않습니다.'))] = None

    @field_validator('executionMode', mode='before')
    @classmethod
    def supported_mode(cls, value):
        if value not in ('PROMPT_BODY', 'CONNECTION_ONLY'):
            raise ApiError(400, 'UNSUPPORTED_EXECUTION_MODE', '현재 PROMPT_BODY와 CONNECTION_ONLY를 지원합니다.')
        return value

    @model_validator(mode='after')
    def validate_configuration(self):
        if self.maxRetries and not self.retrySafe:
            raise ValueError('재시도에는 retrySafe=true가 필요합니다.')
        if len(self.headers) > 20:
            raise ValueError('헤더가 너무 많습니다.')
        seen = set()
        for name, value in self.headers.items():
            lower = header_name(name).lower()
            if lower in seen or lower in RESERVED_HEADERS or any(ord(c) < 32 or ord(c) > 126 for c in value) or len(value) > 2000:
                raise ValueError('사용할 수 없는 헤더입니다.')
            if self.auth.headerName and lower == self.auth.headerName.lower():
                raise ValueError('인증정보는 secret API로 저장합니다.')
            if lower in ('x-api-key', 'api-key', 'apikey', 'x-auth-token'):
                raise ValueError('인증정보는 secret API로 저장합니다.')
            if self.llmKeyHeaderName and lower == self.llmKeyHeaderName.lower():
                raise ValueError('AI API 키는 llm-secret API로 저장합니다.')
            if lower == 'content-type' and value.lower() != 'application/json':
                raise ValueError('JSON 요청만 지원합니다.')
            seen.add(lower)
        if self.llmKeyHeaderName is not None:
            llm = header_name(self.llmKeyHeaderName).lower()
            if llm in RESERVED_HEADERS | {'content-type', 'accept'}:
                raise ValueError('사용할 수 없는 AI API 키 헤더입니다.')
            if self.auth.headerName and llm == self.auth.headerName.lower():
                raise ValueError('AI API 키 헤더는 인증 헤더와 달라야 합니다.')
        encoded = json.dumps(self.requestTemplate, allow_nan=False)
        if len(encoded.encode()) > 256_000:
            raise ValueError('요청 템플릿이 너무 큽니다.')
        read_pointer(self.requestTemplate, self.questionPointer)
        tokens(self.answerPointer)
        for pointer in (self.retrievalPointer, self.citationsPointer):
            if pointer is not None:
                tokens(pointer)
        if self.citationsPointer is not None and self.retrievalPointer is None:
            raise ValueError('citationsPointer를 사용하려면 retrievalPointer가 필요합니다.')
        if self.executionMode == 'PROMPT_BODY':
            if self.promptPointer is None:
                raise ValueError('promptPointer가 필요합니다.')
            read_pointer(self.requestTemplate, self.promptPointer)
            a, b = tokens(self.questionPointer), tokens(self.promptPointer)
            if a[:len(b)] == b or b[:len(a)] == a:
                raise ValueError('질문과 프롬프트 경로가 겹칩니다.')
        elif self.promptPointer is not None:
            raise ValueError('CONNECTION_ONLY는 promptPointer를 사용하지 않습니다.')
        return self


class SecretPut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expectedRevision: Annotated[int, Field(ge=1, strict=True)]
    secret: SecretStr

    @field_validator('secret')
    @classmethod
    def safe_secret(cls, value):
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 8192 or not raw.strip() or any(ord(c) < 32 or ord(c) > 126 for c in raw):
            raise ValueError('인증정보 형식이 올바르지 않습니다.')
        return value


class ConnectionTestCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connectionRevision: Annotated[int, Field(ge=1, strict=True)]
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    promptVersionId: Identifier | None = Field(default=None,
        description='연동 설정이 PROMPT_BODY이면 필수입니다. 없으면 400 VALIDATION_ERROR입니다.')

    @field_validator('question')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('질문이 필요합니다.')
        return value


class AuthResponse(AuthConfig):
    secretConfigured: bool


class ConnectionResponse(BaseModel):
    id: str
    serviceId: str
    revision: int
    executionMode: Literal['PROMPT_BODY', 'CONNECTION_ONLY']
    method: str
    url: str
    headers: dict[str, str]
    requestTemplate: dict
    questionPointer: str
    answerPointer: str
    promptPointer: str | None
    retrievalPointer: str | None = None
    citationsPointer: str | None = None
    auth: AuthResponse
    llmKeyHeaderName: str | None = None
    llmKeyConfigured: bool = False
    timeoutMs: int
    maxRetries: int
    retrySafe: bool
    verifiedRevision: int | None
    createdAt: str
    updatedAt: str


class SecretResponse(BaseModel):
    connectionId: str
    revision: int
    secretConfigured: bool
    verifiedRevision: int | None


class LlmSecretResponse(BaseModel):
    connectionId: str
    revision: int
    llmKeyConfigured: bool
    verifiedRevision: int | None


class ConnectionTestResponse(BaseModel):
    id: str
    connectionRevision: int
    status: Literal['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED']
    answer: str | None
    latencyMs: int | None
    attemptCount: int
    appliedVersionVerified: bool
    error: dict[str, str] | None
    createdAt: str
    finishedAt: str | None
    isCurrentRevision: bool | None = None
