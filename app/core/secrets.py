import json
import os

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import ApiError


def cipher() -> Fernet:
    try:
        return Fernet(os.environ.get('CONNECTION_ENCRYPTION_KEY', '').encode())
    except (ValueError, TypeError):
        raise ApiError(503, 'SECRET_STORAGE_UNAVAILABLE', '인증정보 암호화 설정을 확인해 주세요.') from None


def encrypt_secret(secret: str) -> str:
    return cipher().encrypt(secret.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ApiError(503, 'SECRET_STORAGE_UNAVAILABLE', '인증정보를 복호화할 수 없습니다.') from None


# 연결 하나에 비밀값 두 개(챗봇 인증 키, 답변 생성용 AI 키)를 기존 암호화 컬럼 하나에 함께 저장한다.
# 기존 값(평문 문자열 하나)은 인증 키로 읽어 마이그레이션 없이 호환한다.
PACKED_VERSION = 2


def pack_secrets(auth: str | None, llm: str | None) -> str | None:
    """Encrypt both secrets together; None when neither is set."""
    if auth is None and llm is None:
        return None
    if llm is None:
        # 인증 키만 있으면 기존 형식 그대로 저장해 이전 버전 워커와도 호환
        return encrypt_secret(auth)
    return encrypt_secret(json.dumps({'v': PACKED_VERSION, 'auth': auth, 'llm': llm}))


def unpack_secrets(ciphertext: str | None) -> tuple[str | None, str | None]:
    """Return (auth secret, llm secret) from either the packed or the legacy single-secret form."""
    if not ciphertext:
        return None, None
    plaintext = decrypt_secret(ciphertext)
    if plaintext.startswith('{'):
        try:
            data = json.loads(plaintext)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get('v') == PACKED_VERSION:
            return data.get('auth'), data.get('llm')
    return plaintext, None
