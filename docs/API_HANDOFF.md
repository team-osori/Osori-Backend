# 인증·프로젝트 담당 인계

## 실행과 확인

Python 3.11 이상, MySQL 8.4에서 검증했습니다. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
# DATABASE_URL에 실제 DB 계정과 데이터베이스를 설정합니다.
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

MySQL 데이터베이스는 미리 생성해야 합니다. 비밀번호의 특수 문자는 URL 인코딩하세요. 실제 `.env`는 커밋하지 않습니다. Swagger는 `/docs`, 기계 판독용 스키마는 `/openapi.json`입니다. Swagger의 Authorize에 로그인으로 받은 액세스 토큰을 넣습니다.

## 구현한 API

| Method | 경로 | 입력과 동작 |
| --- | --- | --- |
| POST | `/api/auth/signup` | `loginId`, `name`, `password`로 가입. 201 |
| GET | `/api/auth/login-id-availability?loginId=...` | 아이디 사용 가능 여부 |
| POST | `/api/auth/login` | `loginId`, `password`. `user`, `accessToken`, `refreshToken` 반환 |
| GET | `/api/auth/me` | Bearer 액세스 토큰으로 내 정보 조회 |
| POST | `/api/auth/refresh` | `refreshToken`으로 새 토큰 쌍 발급 |
| POST | `/api/auth/logout` | `refreshToken`의 로그인 세션 폐기. 204, 본문 없음 |
| POST | `/api/projects` | `name`, 선택 `description`. 내 소유 프로젝트 생성. 201 |
| GET | `/api/projects` | 내 프로젝트 목록. `limit` 1~100(기본 20), `cursor` |
| GET | `/api/projects/{projectId}` | 내 프로젝트 상세 |
| POST | `/api/projects/{projectId}/services` | `name`, 선택 `description`으로 챗봇 한 개 등록. 201 |
| GET | `/api/projects/{projectId}/services` | 프로젝트의 챗봇 목록. 0~1개 |
| GET | `/api/projects/{projectId}/services/{serviceId}` | 프로젝트에 속한 챗봇 상세 |

프로젝트와 챗봇 API는 모두 인증이 필요합니다. 챗봇의 API 이름은 노션의 `services`를 유지합니다. 프로젝트 생성 직후 챗봇은 미등록 상태이며 등록 시 DB의 고유 제약으로 두 번째 챗봇을 막습니다. 동시 등록에서도 하나만 성공하고 나머지는 `409 SERVICE_ALREADY_EXISTS`입니다.

프로젝트 수정·삭제와 챗봇 연동 설정은 이번 담당 범위에 포함하지 않았습니다. `revision`은 초기값 1이며, `hasConnection=false`는 아직 연동 설정 기능이 연결되지 않았음을 뜻합니다. 연동 담당자가 실제 설정 상태에 연결해야 합니다.

## 응답과 입력

성공은 `{"data": ...}`입니다. 프로젝트 목록은 `{"data":{"items":[],"nextCursor":null}}`이고 최신 생성 시각·ID 내림차순입니다. 다음 페이지에는 받은 커서를 그대로 전달합니다. 다른 사용자에게 발급된 커서는 거부합니다. 201 응답에는 생성된 리소스의 `Location`이 있습니다.

프로젝트 응답 예시:

```json
{
  "data": {
    "id": "프로젝트 UUID",
    "name": "한빛항공",
    "description": null,
    "revision": 1,
    "serviceCount": 0,
    "createdAt": "2026-11-05T00:00:00Z",
    "updatedAt": "2026-11-05T00:00:00Z"
  }
}
```

아이디는 영문 시작, 영문·숫자·밑줄 4~30자이며 소문자로 통일합니다. 이름은 앞뒤 공백을 제거하고 회원 이름은 1~30자, 프로젝트·챗봇 이름은 1~50자입니다. 비밀번호는 12~128자이며 공백만 불가, 원문을 유지합니다. 비밀번호 확인은 프론트에서 처리합니다. 설명은 생략 또는 null 가능하고 입력 시 앞뒤 공백 제거 후 1~2,000자입니다. 설명 2,000자 제한은 DB 저장 안전성을 위한 현재 구현 기준입니다. 알려지지 않은 Body 필드는 거부합니다.

JSON 쓰기 요청은 `Content-Type: application/json`, 최대 1MiB입니다. 모든 HTTP 응답에는 `X-Request-Id`가 있고 오류 Body의 `requestId`와 같습니다.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "입력값을 확인해 주세요.",
    "requestId": "req_...",
    "fieldErrors": [{"field":"name","code":"REQUIRED","message":"필수 항목입니다."}]
  }
}
```

`fieldErrors`는 입력 검증 오류에서 제공합니다. 비밀번호와 토큰 원문을 오류에 포함하지 않습니다.

| 상태 | 주요 코드 |
| --- | --- |
| 400 | `VALIDATION_ERROR`, `INVALID_CURSOR` |
| 401 | `UNAUTHORIZED`, `INVALID_CREDENTIALS`, `INVALID_REFRESH_TOKEN` |
| 404 | `RESOURCE_NOT_FOUND` — 타인 소유·미존재·프로젝트와 챗봇 소속 불일치 동일 처리 |
| 409 | `LOGIN_ID_ALREADY_EXISTS`, `SERVICE_ALREADY_EXISTS` |
| 413 / 415 | `PAYLOAD_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE` |
| 429 | `RATE_LIMITED`와 `Retry-After` 초 |
| 500 / 503 | `INTERNAL_ERROR` / `SERVICE_UNAVAILABLE` |

## 토큰과 프론트 연동

액세스 토큰은 `Authorization: Bearer <accessToken>`으로 보냅니다. 토큰은 JWT가 아닌 서버 조회 방식의 무작위 토큰입니다. 클라이언트는 내용을 파싱하지 않습니다. 비밀번호는 Argon2, 토큰은 SHA-256 해시만 DB에 저장합니다.

갱신하면 이전 액세스·리프레시 토큰을 폐기합니다. 이미 사용된 리프레시 토큰을 재사용하면 해당 로그인 세션 전체가 폐기됩니다. 동일 계정의 다른 로그인 세션은 유지합니다. 프론트는 동시에 발생한 401을 처리할 때 토큰 갱신 요청을 하나로 합치고, 성공한 새 토큰 쌍을 함께 교체해야 합니다. 갱신이 401이면 재로그인합니다. 중복 갱신·자동 네트워크 재시도도 재사용 판정을 받을 수 있습니다.

현재 기본값은 액세스 30분, 리프레시 7일입니다. 리프레시 유효 기간은 갱신 성공 시점부터 다시 계산합니다. 로그인·갱신은 IP 기준 1분당 각각 10회·30회이며 DB 카운터로 서버 프로세스 사이에 공유합니다. 만료 시각과 같거나 이후면 거부합니다. 서버를 재시작해도 DB가 유지되면 인증 상태가 유지됩니다. 만료 시간·회전 정책·제한 횟수는 노션에서 확정되지 않은 부분에 대한 현재 구현 선택이며 `.env.example`의 값으로 조정할 수 있습니다.

브라우저 토큰 보관 방식은 팀 결정 사항입니다. `CORS_ORIGINS`에 배포한 프론트 주소를 설정하세요. 신뢰할 수 있는 역방향 프록시만 전달 IP를 설정하도록 서버를 구성해야 IP 제한이 정확하게 적용됩니다.

## 다른 백엔드 담당자가 재사용할 코드

```python
from app.api.dependencies import CurrentUser, Db, OwnedProject

# 경로 매개변수 이름은 projectId를 사용합니다.
@router.get('/api/projects/{projectId}/policies')
def list_policies(project: OwnedProject, db: Db):
    # project는 인증 및 소유권 검사가 완료된 Project입니다.
    ...
```

프로젝트 하위 리소스는 소유권 검사에 더해 `resource.project_id == project.id`도 검사하세요. 공통 예외는 `app.core.errors.ApiError(status, code, message)`를 사용합니다. DB 세션은 `Db`, 사용자만 필요하면 `CurrentUser`를 사용합니다. 공통 응답 모델은 `app.schemas.responses`에 있습니다.

## 검증과 DB 관리

```bash
.venv/bin/python -m pytest -q
# 전용 임시 MySQL DB에만 실행합니다. 테스트 데이터를 생성합니다.
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@127.0.0.1:3306/osori_test?charset=utf8mb4' .venv/bin/python -m pytest -q tests/test_mysql_auth_integration.py
.venv/bin/python -m alembic check
# 만료된 세션과 오래된 호출 카운터 정리. 세션의 토큰 이력도 함께 삭제됩니다.
.venv/bin/python -m app.maintenance
```

정리 명령은 자동 예약되어 있지 않습니다. 운영 환경에서 실행 주기를 정하면 됩니다. 마이그레이션은 `migrations/versions`에서 관리합니다. 예전 개발 코드로 테이블을 자동 생성한 DB는 적용 전 스키마를 비교하세요. 이미 있는 테이블에 무조건 stamp하거나 삭제하지 않습니다.

참조: [노션 API 명세](https://app.notion.com/p/API-40ed7f2f55af80c59a0bf813c73f6de9), [API 데모 명세](https://app.notion.com/p/40ed7f2f55af80e4a2bcf0ee70549e45). 이 문서는 위 범위에 대해 현재 구현한 연동 계약입니다.
