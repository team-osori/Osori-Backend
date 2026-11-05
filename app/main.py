from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import CORS_ORIGINS
from fastapi.exceptions import RequestValidationError

from app.api.auth import router
from app.api.projects import router as project_router
from app.api.connections import router as connection_router
from app.api.prompts import router as prompt_router
from app.core.errors import ApiError
from starlette.exceptions import HTTPException
from sqlalchemy.exc import OperationalError
from app.core.errors import RequestIdMiddleware, error_response
from app.core.request_limits import AuthRequestLimits


app = FastAPI(title='Osori API', version='0.1.0')
app.add_middleware(AuthRequestLimits)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'], allow_headers=['Authorization', 'Content-Type', 'If-Match', 'Idempotency-Key'], expose_headers=['X-Request-Id', 'Location', 'Retry-After'])
app.include_router(router)
app.include_router(project_router)
app.include_router(connection_router)
app.include_router(prompt_router)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """Render expected domain errors without leaking internal exception details."""
    return error_response(request.scope, exc.status, exc.code, exc.message, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Return safe field-level errors without including submitted values."""
    field_errors = []
    for error in exc.errors():
        location = error.get('loc', ())
        field = '.'.join(str(part) for part in location[1:]) or 'body'
        code = 'REQUIRED' if error.get('type') == 'missing' else 'INVALID'
        field_errors.append({'field': field, 'code': code, 'message': '필수 항목입니다.' if code == 'REQUIRED' else '유효하지 않은 값입니다.'})
    return error_response(request.scope, 400, 'VALIDATION_ERROR', '입력값을 확인해 주세요.', field_errors=field_errors)


@app.get('/api/health', tags=['health'])
def health() -> dict[str, str]:
    """Report process liveness without requiring database availability."""
    return {'status': 'ok'}


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    """Wrap framework HTTP errors in the common API error format."""
    code = 'RESOURCE_NOT_FOUND' if exc.status_code == 404 else 'HTTP_ERROR'
    return error_response(request.scope, exc.status_code, code, '요청을 처리할 수 없습니다.', headers=exc.headers)


@app.exception_handler(OperationalError)
async def unavailable_handler(request: Request, exc: OperationalError):
    """Return a retryable service error without database connection details."""
    return error_response(request.scope, 503, 'SERVICE_UNAVAILABLE', '일시적으로 서비스를 사용할 수 없습니다.')


def custom_openapi():
    """Publish the actual 400 validation contract instead of FastAPI's default 422 response."""
    if app.openapi_schema is None:
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path in schema['paths'].values():
            for operation in path.values():
                if isinstance(operation, dict):
                    operation.get('responses', {}).pop('422', None)
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
