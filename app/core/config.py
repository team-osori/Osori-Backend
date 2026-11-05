import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path('.env'))

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL을 설정해 주세요. .env.example을 참고하세요.')
ACCESS_TOKEN_MINUTES = int(os.getenv('ACCESS_TOKEN_MINUTES', '30'))
REFRESH_TOKEN_DAYS = int(os.getenv('REFRESH_TOKEN_DAYS', '7'))
# Temporary team defaults; adjust to deployment traffic and proxy configuration.
AUTH_RATE_WINDOW_SECONDS = int(os.getenv('AUTH_RATE_WINDOW_SECONDS', '60'))
AUTH_LOGIN_RATE_LIMIT = int(os.getenv('AUTH_LOGIN_RATE_LIMIT', '10'))
AUTH_REFRESH_RATE_LIMIT = int(os.getenv('AUTH_REFRESH_RATE_LIMIT', '30'))
if min(ACCESS_TOKEN_MINUTES, REFRESH_TOKEN_DAYS, AUTH_RATE_WINDOW_SECONDS,
       AUTH_LOGIN_RATE_LIMIT, AUTH_REFRESH_RATE_LIMIT) <= 0:
    raise RuntimeError('토큰 만료 시간과 호출 제한은 양수여야 합니다.')

CORS_ORIGINS = [origin.strip() for origin in os.getenv('CORS_ORIGINS', 'http://localhost:5173').split(',') if origin.strip()]
