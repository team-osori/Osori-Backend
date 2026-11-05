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

프로젝트 수정·삭제는 이번 담당 범위에 포함하지 않았습니다. 연동 설정은 별도 [연동·프롬프트 구현](CONNECTION_PROMPT_HANDOFF.md)에 연결했습니다. `hasConnection`은 실제 설정 존재 여부이며 테스트 통과 여부는 연결 설정의 `verifiedRevision`으로 확인합니다. 서비스 revision과 연결 revision은 별개입니다.

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

현재 기본값은 액세스 30분, 리프레시 7일입니다. 리프레시 유효 기간은 갱신 성공 시점부터 다시 계산하되 로그인 후 최대 30일을 넘지 않습니다. 세션당 갱신 횟수도 최대 1,000회로 제한하며 도달 후 다시 갱신을 요청하면 세션을 폐기합니다. 사용한 토큰 이력은 세션이 끝날 때까지 유지하므로 재사용 감지가 유지됩니다. 회원가입·로그인·갱신은 IP 기준 1분당 각각 10회·10회·30회이며 DB 카운터로 서버 프로세스 사이에 공유합니다. 만료 시각과 같거나 이후면 거부합니다. 서버를 재시작해도 DB가 유지되면 인증 상태가 유지됩니다. 만료 시간·회전 정책·제한 횟수는 노션에서 확정되지 않은 부분에 대한 현재 구현 선택이며 `.env.example`의 값으로 조정할 수 있습니다.

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

정리 명령은 자동 예약되어 있지 않습니다. 운영 환경에서 실행 주기를 정하면 됩니다. 마이그레이션은 `migrations/versions`에서 관리합니다. `20261105_04`는 기존 세션의 최대 수명을 기존 리프레시 만료 시각으로 설정하고, 저장된 토큰 이력 수로 갱신 횟수를 복원합니다. 예전 개발 코드로 테이블을 자동 생성한 DB는 적용 전 스키마를 비교하세요. 이미 있는 테이블에 무조건 stamp하거나 삭제하지 않습니다.

참조: [노션 API 명세](https://app.notion.com/p/API-40ed7f2f55af80c59a0bf813c73f6de9), [API 데모 명세](https://app.notion.com/p/40ed7f2f55af80e4a2bcf0ee70549e45). 이 문서는 위 범위에 대해 현재 구현한 연동 계약입니다.

## 커서 입력 검증

프로젝트·프롬프트·정책·테스트셋·평가 조회는 공통 검증 함수를 사용합니다. 기존 커서 JSON 형식은 유지합니다. 잘못된 커서는 `400 INVALID_CURSOR`이며 DB 내부 오류로 넘기지 않습니다. 중복 JSON 키·NaN·Infinity·여분 필드, 불리언 형식 버전, 비 ASCII·제어문자·유효하지 않은 유니코드 ID, 비정규/시간대 포함/1000년 이전 날짜, signed INT 범위 밖의 버전 숫자를 거부합니다. 프롬프트 버전 커서의 before도 JSON 정수 1~2,147,483,647만 허용합니다.

## 프로젝트 진행 요약 (#16)

프로젝트 생성·단건·목록 응답에 `progress`를 포함합니다. 별도 프로젝트별 요청 없이 목록에서 단계·최신 시험지·최신 평가·사람 검토 수를 함께 표시할 수 있습니다.

- `stage`: 최신 평가가 COMPLETED/PARTIAL_FAILED이면 3(오답 노트), 평가 실행이 있거나 확정 시험지가 있으면 2(채점), 나머지는 1(시험지).
- `unlockedStage`: 확정 시험지가 있으면 2, 완료 또는 부분 실패한 평가 이력이 있으면 3. 최신 초안을 만들거나 재평가가 진행·실패해도 과거 이력으로 열린 단계는 유지합니다.
- `status`: 준비만 진행 중이면 SETUP, 확정 시험지 또는 실행/재시도 단계는 RUNNING, 최신 평가가 COMPLETED/PARTIAL_FAILED이면 DONE입니다. DONE은 평가 실행 종료를 뜻하며 사람 검토 완료 여부는 `reviewedQuestions`로 별도 확인합니다. FAILED는 2단계 RUNNING(재시도 필요), 실제 상태는 `evaluation.status`로 구분합니다.
- `testSet`: 프로젝트 전체에서 가장 최근 생성한 시험지 버전의 `versionId`, `label`(vN), `status`, `questionCount`. 없으면 null입니다.
- `evaluation`: 최신 접수 평가의 `id/status/completedQuestions/totalQuestions/reviewedQuestions`. 없으면 null입니다. `reviewedQuestions`는 모든 기대 조건에 최신 사람 pass/fail 판정이 있는 문항 수입니다. COMPARE는 A/B 양쪽 모두 필요하며, 일부 조건만 검토했거나 최신 판정이 not_evaluable이면 제외합니다. 반복 수정 이력은 중복 계산하지 않습니다.
- `lastEvaluatedAt`: 가장 최근 평가 종료 시각(실패 포함), 없으면 null. `promptVersion`: 최신 평가 A측에 명시적으로 선택된 프롬프트 버전 번호, 기업 설정 프롬프트 사용 또는 평가 없음은 null.

최신 항목은 생성 시각·ID로 결정하며 같은 시각의 시험지는 버전 번호를 먼저 비교합니다. 목록에 포함된 소유 프로젝트만 일괄 집계하고 과거 평가 스냅샷·답변·인증정보를 로드하지 않습니다. DB 마이그레이션은 필요 없습니다.

## 프로젝트·서비스 메타데이터 수정 (#20 A 일부)

`PATCH /api/projects/{projectId}`와 `PATCH /api/projects/{projectId}/services/{serviceId}`는 최신 `expectedRevision`과 변경할 `name` 또는 `description`을 받습니다. name은 앞뒤 공백 제거 후 1~50자, description은 최대 2,000자입니다. 생략한 필드는 유지하고 `description: null`은 설명을 삭제합니다. 이름은 null이나 공백일 수 없고, 변경 필드를 하나 이상 보내야 합니다.

성공 시 200과 해당 자원의 최신 응답(revision + 1)을 반환합니다. 이전 revision은 `409 REVISION_CONFLICT`, 다른 소유자·잘못된 부모 경로는 404입니다. 서비스 수정에는 서비스의 revision을 사용하며 연동 설정 revision이나 프로젝트 revision을 변경하지 않습니다.

설정 저장과 이름 변경 외에 아래의 휴지통·멤버 기능과 평가 인계 문서의 중단/재개·분석 지원 상태를 함께 제공합니다.

## 프로젝트 휴지통·리소스 삭제·멤버 (#20)

### 삭제와 복구

- `DELETE /api/projects/{projectId}`: admin 권한과 `If-Match: "revision"` 필요. 204, revision 증가, 즉시 목록·하위 API에서 숨기고 실행/대기 평가와 연결 테스트를 중단합니다.
- `GET /api/projects/trash`: 소유자 자신의 휴지통만 반환합니다(`deletedAt`, `purgeAfter`, `revision`). 멤버에게 복구 권한을 주지 않습니다.
- `POST /api/projects/{projectId}/restore`: 소유자가 `{"expectedRevision": N}`으로 삭제 후 30일 이내 복구합니다. 중단한 평가를 자동으로 재시작하지 않습니다. 기한 경과는 `409 RESTORE_EXPIRED`입니다.
- 삭제 30일 후 `python -m app.maintenance`가 연결·프롬프트·정책·시험지·평가·검토·초대까지 해당 프로젝트의 DB 데이터를 정리합니다. 평가 워커가 시작할 때와 매 시간 이 정리를 자동 실행합니다. 워커가 중지된 동안 정리는 지연되며 재시작 시 처리합니다.
- `PATCH …/prompts/{promptId}`, `PATCH …/testsets/{testSetId}`: `expectedRevision`, `name`으로 이름을 변경합니다. 원문 버전 번호와 이름 변경 revision을 분리합니다.
- `DELETE …/prompts/{promptId}`, `DELETE …/testsets/{testSetId}`: 최신 따옴표 revision을 If-Match로 보내 논리 삭제합니다. 새 작업에서 선택할 수 없지만 이미 저장된 평가의 원문·결과·근거 스냅샷은 보존합니다.

### 프로젝트 단위 권한

기존 프로젝트 소유자는 항상 admin이며 제거하거나 강등할 수 없습니다. admin은 프로젝트 이름/설명·서비스 이름/설명·프로젝트 삭제·멤버 초대/역할/제거를 관리합니다. 수락한 member는 프로젝트 목록·조회와 정책/시험지/프롬프트/연동/평가 작업에 참여합니다. 미수락 초대는 접근 권한을 주지 않습니다. 리소스 권한 없음은 404, 멤버가 관리자 작업을 요청하면 `403 ADMIN_REQUIRED`입니다.

- `GET /api/projects/{projectId}/members`: 소유자·수락 멤버·대기 초대 목록. `userId/loginId/name/role/status/revision/lastSeenAt`.
- `POST /api/projects/{projectId}/invitations`: admin이 `{"loginId":"가입된아이디","role":"member"}`로 초대합니다. role은 admin/member. 기존 계정만 지원합니다. 프로젝트당 소유자 포함 200명 제한입니다.
- `GET /api/invitations`: 로그인 사용자의 미수락 초대 목록.
- `POST /api/invitations/{invitationId}/accept`: 초대받은 사용자가 `expectedRevision`으로 수락.
- `PATCH /api/projects/{projectId}/members/{userId}`: admin이 `expectedRevision`, `role`로 변경.
- `DELETE /api/projects/{projectId}/members/{userId}`: admin이 If-Match로 제거하거나 미수락 초대를 취소. 삭제 후 다음 요청부터 접근 차단.
- `GET /api/auth/me`: `projectRoles`로 프로젝트별 실제 역할을 반환합니다. 전역 역할은 반환하지 않습니다. `lastSeenAt`은 로그인 또는 인증된 접근 기준이며 과도한 DB 쓰기를 피하도록 최대 5분 간격으로 갱신합니다.

마이그레이션 `20261105_10`이 기존 데이터에 nullable 접근/삭제 시각과 리소스 deleted=false 기본값을 추가합니다. 기존 소유자 권한과 데이터는 유지합니다.

## 아이디 인증 전환 (#39)

회원가입은 `loginId`, `name`, `password`, 로그인은 `loginId`, `password`만 사용합니다. `loginId`는 영문자로 시작하는 4~30자의 영문·숫자·밑줄이며 소문자로 정규화합니다. 비밀번호는 12~128자입니다. 중복 아이디는 `409 LOGIN_ID_ALREADY_EXISTS`입니다.

프론트엔드는 인증 코드 요청·입력 단계를 제거하고 중복 확인을 `/api/auth/login-id-availability?loginId=...`로 바꿉니다. 프로젝트 초대 요청도 `loginId`를 사용합니다. 가입·로그인·내 정보·멤버 응답에는 `loginId`가 있으며 `email` 필드는 없습니다. 이전 `/email-verifications`, `/email-availability`, `/legacy-email-link` 경로는 404이며, 폐기된 이메일/인증 코드 필드를 요청에 보내면 400입니다.

마이그레이션 `20261106_39`는 사용자 ID·login_id·비밀번호 해시·세션·프로젝트·멤버십을 보존합니다. 과거 마이그레이션 파일은 적용 이력 호환을 위해 유지하고 후속 마이그레이션에서 이메일 컬럼과 인증 테이블을 제거합니다. downgrade는 빈 스키마만 복원하며 삭제한 주소·인증 코드는 복원하지 않으므로 이전 앱으로 돌아가려면 배포 전 DB 백업을 검토해야 합니다.
