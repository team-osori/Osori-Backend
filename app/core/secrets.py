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
