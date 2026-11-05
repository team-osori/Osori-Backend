from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

LoginId = Annotated[str, Field(min_length=4, max_length=30, pattern=r'^[A-Za-z][A-Za-z0-9_]*$'), AfterValidator(str.lower)]
Password = Annotated[str, Field(min_length=12, max_length=128)]
Token = Annotated[str, Field(min_length=1, max_length=4096)]


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    loginId: LoginId
    name: Annotated[str, Field(min_length=1, max_length=30)]
    password: Password

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        """Normalize names before enforcing their length constraints."""
        return value.strip() if isinstance(value, str) else value

    @field_validator('name', 'password')
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject whitespace-only credentials without altering their original characters."""
        if not value.strip():
            raise ValueError('공백만 입력할 수 없습니다.')
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    loginId: LoginId
    password: Password

    @field_validator('password')
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject whitespace-only credentials without altering their original characters."""
        if not value.strip():
            raise ValueError('공백만 입력할 수 없습니다.')
        return value


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    refreshToken: Token

    @field_validator('refreshToken')
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject whitespace-only credentials without altering their original characters."""
        if not value.strip():
            raise ValueError('공백만 입력할 수 없습니다.')
        return value
