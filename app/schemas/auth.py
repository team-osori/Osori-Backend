from typing import Annotated
import re

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

def normalize_email(value: str) -> str:
    """Use one explicit, case-insensitive ASCII email identity without DNS lookups."""
    value = value.strip().lower()
    if len(value) > 254 or not value.isascii() or value.count('@') != 1:
        raise ValueError('유효한 이메일 주소를 입력해 주세요.')
    local, domain = value.split('@')
    labels = domain.split('.')
    if (not local or len(local) > 64 or not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
        or local.startswith('.') or local.endswith('.') or '..' in local or len(labels) < 2
        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise ValueError('유효한 이메일 주소를 입력해 주세요.')
    return value


Email = Annotated[str, Field(min_length=3, max_length=254), AfterValidator(normalize_email)]
Password = Annotated[str, Field(min_length=12, max_length=128)]
Token = Annotated[str, Field(min_length=1, max_length=4096)]


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: Email
    name: Annotated[str, Field(min_length=1, max_length=30)]
    password: Password
    verificationId: Annotated[str, Field(min_length=36, max_length=36)]
    verificationCode: Annotated[str, Field(pattern=r'^[0-9]{6}$')]

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
    email: Email
    password: Password

    @field_validator('password')
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject whitespace-only credentials without altering their original characters."""
        if not value.strip():
            raise ValueError('공백만 입력할 수 없습니다.')
        return value


class LegacyEmailLink(BaseModel):
    """Credentials for a legacy account and the email to attach to it."""
    model_config = ConfigDict(extra='forbid')
    loginId: Annotated[str, Field(min_length=4, max_length=30, pattern=r'^[A-Za-z][A-Za-z0-9_]*$'), AfterValidator(str.lower)]
    password: Password
    email: Email
    verificationId: Annotated[str, Field(min_length=36, max_length=36)]
    verificationCode: Annotated[str, Field(pattern=r'^[0-9]{6}$')]


class VerificationRequest(BaseModel):
    """Request proof of control of a normalized email address."""
    model_config = ConfigDict(extra='forbid')
    email: Email


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
