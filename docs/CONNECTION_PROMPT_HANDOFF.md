# 챗봇 연동·프롬프트 담당 인계

## 기준과 범위

2026-11-05 사용자가 지정한 [오소리 API 명세서 · 데모](https://app.notion.com/p/40ed7f2f55af80e4a2bcf0ee70549e45?v=40ed7f2f55af80ef84c8000cd6400a21)의 **CN-01~06, PR-01~06**을 구현했습니다. 로그인된 브라우저에서 명세를 확인했습니다. 첨부된 이전 인계의 자체 RAG 방향과 달리 이 명세의 기업 챗봇 URL 연동을 기준으로 합니다. Notion은 수정하지 않았습니다.

연결 설정 화면에서는 주소·요청 템플릿·인증정보를 저장하고 질문 하나를 전송해 답변 또는 실행 오류를 확인합니다. 프롬프트 화면에서는 원문 등록 및 수정본의 새 버전 저장을 수행합니다. A/B 실행 화면은 버전 목록·상세에서 같은 프롬프트의 서로 다른 두 버전을 선택합니다.

평가 실행·채점·정책 문서·RAG 구축은 다른 담당 영역입니다. 버전 간 문장 차이는 PR-06으로 두 원문을 조회해 프론트에서 표시합니다. 데모 명세에 별도의 비교·선택 저장 API는 없어 추가하지 않았습니다.

## 실행

기존 README의 가상환경·DB 설정 후 저장소 루트에서 실행합니다.

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

**별도 터미널에서 워커도 실행해야 합니다.** API 서버는 DB에 작업을 접수하고 워커가 기업 챗봇을 호출합니다. 워커가 없으면 `QUEUED`로 유지됩니다.

```bash
.venv/bin/python -m app.connection_worker
# 대기 작업을 최대 한 건 처리하고 종료:
.venv/bin/python -m app.connection_worker --once
```

인증정보 저장 전에 아래 명령으로 Fernet 키를 생성해 `.env`의 `CONNECTION_ENCRYPTION_KEY`에 설정합니다. API 서버와 워커는 같은 DB·암호화 키를 사용합니다.

```bash
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

키와 `.env`는 커밋하지 않습니다. 실행할 때마다 키를 바꾸면 기존 인증정보를 복호화할 수 없습니다. 키 교체에 따른 재암호화는 별도 운영 작업입니다. 키 누락·형식 오류 시 비밀값 저장은 `503 SECRET_STORAGE_UNAVAILABLE`입니다. `NONE` 인증은 암호화 키 없이 사용할 수 있습니다.

## 연결 API

기본 경로는 `/api/projects/{projectId}/services/{serviceId}`입니다. 기존 Bearer 액세스 토큰·프로젝트 소유권 검사를 재사용하며 타인 소유·소속 불일치는 404입니다.

| ID | Method / 하위 경로 | 동작 |
| --- | --- | --- |
| CN-01 | `GET /connection` | 설정 조회. 미등록 404. 비밀값 대신 `auth.secretConfigured` 반환 |
| CN-02 | `PUT /connection` | 전체 설정 저장. 최초 expectedRevision=0, 교체 시 현재 revision |
| CN-03 | `PUT /connection/secret` | expectedRevision·secret으로 인증정보 등록·교체 |
| CN-04 | `DELETE /connection/secret` | 현재 revision의 `If-Match: "2"` 필요. 성공 204 |
| CN-05 | `POST /connection-tests` | Idempotency-Key 필수. 202 및 조회 Location 반환 |
| CN-06 | `GET /connection-tests/{connectionTestId}` | 상태·답변·처리 시간·시도 횟수·오류 조회 |

최초 설정 예시:

```json
{
  "expectedRevision": 0,
  "executionMode": "PROMPT_BODY",
  "method": "POST",
  "url": "https://chatbot.example.com/chat",
  "headers": {"Content-Type": "application/json"},
  "requestTemplate": {"question": "", "systemPrompt": ""},
  "questionPointer": "/question",
  "answerPointer": "/answer",
  "promptPointer": "/systemPrompt",
  "auth": {"type": "BEARER"},
  "timeoutMs": 10000,
  "maxRetries": 0,
  "retrySafe": false
}
```

example.com 주소는 설명용이며 실제 기업 테스트 API로 교체해야 합니다. 이번 작업에서는 실제 기업 인증정보·서버를 제공받지 않아 기업 서버 호출을 수행하지 않았습니다.

설정 저장 후 revision은 1, 인증정보 저장 후 2입니다. 연결 테스트에는 이 값을 보냅니다.

```json
{
  "connectionRevision": 2,
  "question": "학생운임 조건이 궁금합니다.",
  "promptVersionId": "저장된-프롬프트-버전-ID"
}
```

상태는 `QUEUED → RUNNING → SUCCEEDED | FAILED`입니다. 실행 실패는 error에 기록되며 정책 위반 판정과 다릅니다. 정상 답변 수신 시 **해당 revision만** verifiedRevision에 기록합니다. 외부 챗봇의 실제 프롬프트 적용은 검증할 수 없어 appliedVersionVerified는 false입니다.

설정·인증정보 변경은 revision을 증가시키고 검증 상태를 초기화합니다. URL 또는 인증 방식·헤더 이름을 바꾸면 기존 비밀값도 제거합니다. 동일 수신처·인증 방식의 일반 설정 수정은 비밀값을 보존합니다. 비밀값 삭제의 If-Match 누락은 428, 잘못된 형식은 400, 오래된 revision은 412입니다.

동일 사용자·서비스·멱등키의 동일 요청은 기존 작업, 다른 Body는 `409 IDEMPOTENCY_CONFLICT`입니다. 완료된 작업의 키는 접수 24시간 이후 재사용할 수 있고 결과는 보존합니다. 진행 중인 키는 만료시키지 않습니다. 원자적 상태 변경으로 여러 워커의 중복 실행을 방지합니다. 중단된 RUNNING 작업은 240초 이후 워커가 WORKER_INTERRUPTED로 종료합니다. 호출 여부가 불명확하므로 자동 재실행하지 않으며 새 멱등키로 다시 접수합니다.

## 프롬프트 API

| ID | Method / 하위 경로 | 동작 |
| --- | --- | --- |
| PR-01 | `POST /prompts` | content 필수. name 생략 시 `기본 프롬프트` (명시한 공백 이름은 거절). 선택 label/changeNote/externalVersionRef. 첫 버전 포함 201 |
| PR-02 | `GET /prompts` | 최신 생성 순 목록. limit 1~100(기본 20), cursor |
| PR-03 | `GET /prompts/{promptId}` | 최신 프롬프트·currentVersion |
| PR-04 | `POST /prompts/{promptId}/versions` | baseVersionId·content 및 선택 메타데이터. 새 버전 201 |
| PR-05 | `GET /prompts/{promptId}/versions` | 최신 버전 번호 순 목록. limit/cursor |
| PR-06 | `GET /prompts/{promptId}/versions/{promptVersionId}` | 특정 버전 원문 |

원문 공백·개행을 그대로 보존합니다. 1~100,000자이며 공백만 있는 원문은 거부합니다. 한글 100,000자 저장을 위해 MySQL MEDIUMTEXT를 사용합니다. 이전 버전 수정 API는 없습니다. 버전 생성과 최신 포인터 갱신은 하나의 트랜잭션입니다.

오래된 baseVersionId는 `409 PROMPT_VERSION_CONFLICT`, 원문·externalVersionRef가 모두 같으면 `409 NO_CHANGES`입니다. 레이블만 바꿔서는 버전을 추가하지 않습니다. 선택 메타데이터 생략 시 새 버전에는 null을 저장합니다.

## 평가 담당자 연동

```python
from app.services.prompts import select_ab_versions
from app.services.chatbot_gateway import execute_chatbot

# 소유권 검사 완료 후 호출합니다. 서비스 소속·동일 프롬프트·서로 다른 버전을 검사합니다.
a, b = select_ab_versions(db, service.id, a_version_id, b_version_id)

# 동일 config·question을 사용합니다. 비밀값은 출력하거나 스냅샷에 넣지 않습니다.
result_a = await execute_chatbot(config, question, a.content, decrypted_secret)
result_b = await execute_chatbot(config, question, b.content, decrypted_secret)
```

평가 접수에서는 연결 revision·verifiedRevision 일치, PROMPT_BODY 모드, 테스트셋·정책·평가 기준 준비 여부를 추가로 검사해야 합니다. 평가 입력·검색 자료·설정 스냅샷은 평가 담당 영역입니다. 호출부는 질문·A/B 사이의 쿠키와 세션을 공유하지 않습니다.

## 현재 구현 선택·제한

다음은 데모 계약을 구현하기 위한 선택이며 팀 확정 요구사항으로 간주하지 않습니다.

- 모드: PROMPT_BODY, 연결만 확인하는 CONNECTION_ONLY. VERSION_BODY·DUAL_ENDPOINT 등은 UNSUPPORTED_EXECUTION_MODE. 외부 버전 참조는 저장만 합니다.
- 인증: NONE·BEARER·사용자 지정 헤더 HEADER. 비밀값은 전용 secret API로만 등록합니다. URL·템플릿·일반 헤더·프롬프트에 넣지 않습니다.
- 호출: HTTPS 443·POST·JSON. URL 사용자정보·fragment, 사설·루프백·링크 로컬·멀티캐스트 주소를 차단합니다. 실제 소켓에 쓰는 DNS 결과를 검사합니다. 리다이렉트·환경 프록시·쿠키 저장을 사용하지 않고 TLS 인증서를 검증합니다.
- 경로: RFC 6901 JSON Pointer. 루트가 아닌 기존 객체 키·배열 인덱스에 대입합니다. 질문·프롬프트 경로는 겹칠 수 없습니다.
- 제한 시간: 시도당 DNS·연결·본문 수신 전체 100~60,000ms. 재시도 0~2회, retrySafe=true일 때만 허용. 인증 실패·응답 구조 오류는 재시도하지 않습니다.
- 응답: 비압축 JSON 최대 1MiB, 답변 문자열 1~100,000자. 외부 오류 본문·예외 메시지는 반환·로깅하지 않습니다. 정상 답변에 인증 비밀값이 그대로 포함되면 마스킹합니다.
- 저장 제한: label 100자, changeNote 2,000자, externalVersionRef 200자, 이름 100자. 헤더 20개·값당 ASCII 2,000자, secret ASCII 1~8,192자. 직렬화 요청 템플릿 256,000바이트.
- 워커 프로세스 관리·알림·큐 적체 감시·암호화 키 교체 운영은 별도 구성합니다.

실행 오류: UPSTREAM_TIMEOUT, UPSTREAM_AUTH_FAILED, UPSTREAM_RATE_LIMITED, UPSTREAM_HTTP_ERROR, UPSTREAM_CONNECTION_FAILED, INVALID_UPSTREAM_RESPONSE, UPSTREAM_RESPONSE_TOO_LARGE, UNSAFE_ENDPOINT, CONNECTION_CHANGED, WORKER_INTERRUPTED.

## 검증

```bash
.venv/bin/python -m pytest -q
# 반드시 테스트 전용 임시 DB를 지정합니다.
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@localhost/osori_test?charset=utf8mb4' \
  .venv/bin/python -m pytest -q tests/test_mysql_connections_integration.py
```

SQLite 테스트는 HTTP API·DB·워커를 연결하고 외부 호출만 대체합니다. MySQL/MariaDB 통합 테스트는 동시 저장·교체·중복 접수·워커 선점을 검증합니다. 호출부 테스트는 주소·DNS 검사·시간 제한·재시도·JSON 경로·오류 상태·응답 크기를 확인합니다.

이번 검증 결과: 기본 테스트 83개 통과(별도 DB 테스트 12개 제외), 임시 MariaDB 11.8에서 통합 테스트 12개 통과. MySQL 서버 자체는 로컬 설치의 라이브러리 문제로 실행되지 않아 MySQL 실서버 검증은 수행하지 못했습니다. 마이그레이션 적용·롤백과 모델 대비 차이 검사는 SQLite에서 통과했습니다. 실제 기업 서버 연결은 별도 확인이 필요합니다.

구현 참고: [aiohttp](https://docs.aiohttp.org/en/stable/client_advanced.html), [Fernet](https://cryptography.io/en/latest/fernet/).
