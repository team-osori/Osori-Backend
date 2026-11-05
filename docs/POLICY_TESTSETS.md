# 정책·테스트셋 API 인계

## 이번 범위

텍스트 정책 원문과 규칙을 저장하고, 정책 버전을 근거로 질문·기대 행동·금지 행동을 작성하여 사람이 검토·확정하는 기능입니다. PDF 업로드는 이번 범위에 포함하지 않습니다.

Bedrock 질문 생성 어댑터를 연결했습니다. 기본 모델은 Claude Haiku 4.5이며 서버 IAM 역할을 사용합니다. `QUESTION_GENERATION_PROVIDER=bedrock`으로 활성화하고 개인 API 키를 사용하지 않습니다. 미설정 시 `503 GENERATION_NOT_CONFIGURED`이며 가짜 질문은 만들지 않습니다. 설정은 [Bedrock 연동](BEDROCK_SETUP.md), 조건·이력 계약은 [A 기능 인계](QUESTION_GENERATION_HANDOFF.md)에 있습니다.

## 실행

기존 인증·프로젝트 환경에서 다음을 실행합니다. 정책 기본 마이그레이션은 `20261105_06`이며, 질문 이력 확장은 평가 마이그레이션 `07` 다음의 `20261105_08`입니다. PR 병합 전 정책 전용 개발 DB에서 이전 `05`를 적용했다면 revision 번호만 보고 자동 stamp하지 말고, 테이블 구성을 확인한 뒤 전용 개발 DB를 새로 준비하세요.

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Swagger `/docs`에서 요청·응답 스키마를 확인할 수 있습니다. 아래 API는 모두 프로젝트 소유자의 액세스 토큰이 필요합니다. 다른 사용자·다른 프로젝트·잘못된 상위 경로의 리소스는 404입니다. 커서는 서버가 반환한 값을 그대로 사용하며, 잘못된 타입·범위·날짜·중복 JSON 필드는 400 INVALID_CURSOR로 거부합니다. 프로젝트 단위로 관리하며, ②의 챗봇 연동·프롬프트 기능과 독립적으로 정책을 먼저 준비할 수 있습니다.

## 정책

기준 경로는 `/api/projects/{projectId}/policies`입니다.

| Method | 뒤에 붙는 경로 | 동작 |
| --- | --- | --- |
| POST | 없음 | 정책과 첫 버전 생성. 201, Location |
| GET | 없음 | 활성 정책 목록. limit/cursor |
| GET | `/{policyId}` | 정책 메타데이터와 최신 원문 버전 |
| POST | `/{policyId}/versions` | 새 원문·규칙 버전 추가. expectedRevision 필요 |
| GET | `/{policyId}/versions` | 버전 목록. version 내림차순, limit/cursor |
| GET | `/{policyId}/versions/{policyVersionId}` | 지정 버전 원문·규칙 조회 |
| DELETE | `/{policyId}` | 논리 삭제. If-Match: "revision" 필요 |

최초 생성 예시:

```json
{
  "name": "예약 취소 정책",
  "content": "예약 취소는 출발 24시간 전까지 가능합니다.\n임의 보상은 금지합니다.",
  "rules": [
    {
      "key": "cancellation-deadline",
      "description": "출발 24시간 전까지 취소 가능하다고 안내한다.",
      "sourceQuote": "예약 취소는 출발 24시간 전까지 가능합니다."
    }
  ]
}
```

생성 응답은 `data.policy`와 `data.version`입니다. 이후 버전을 추가할 때는 `name`을 제외하고 `expectedRevision`을 넣습니다. 기존 버전은 수정하지 않습니다. 원문과 규칙 문장은 공백·줄바꿈을 보존합니다. 규칙 key는 한 버전 내에서 중복할 수 없고 sourceQuote는 원문에 실제로 포함돼야 합니다.

현재 구현 제한: 이름 1~100자(앞뒤 공백 제거), 원문 1~100,000자, 규칙 1~100개, 규칙 설명 2,000자, 근거 인용 10,000자입니다. JSON 요청은 공통 1MiB 제한을 따릅니다. 모두 공백만인 문자열은 거부합니다.

삭제는 원문을 지우지 않습니다. 삭제한 정책은 새 테스트셋의 입력으로 선택할 수 없지만, 기존 초안·확정본이 참조한 버전과 근거는 유지됩니다.

## 테스트셋

기준 경로는 `/api/projects/{projectId}/testsets`입니다.

| Method | 뒤에 붙는 경로 | 동작 |
| --- | --- | --- |
| POST | 없음 | 이름과 policyVersionIds로 첫 초안 생성 |
| GET | 없음 | 테스트셋 목록. limit/cursor |
| GET | `/{testSetId}` | 메타데이터와 최신 버전·문항 |
| GET | `/{testSetId}/versions` | 버전 요약 목록. limit/cursor, 문항 본문 제외 |
| GET | `/{testSetId}/versions/{versionId}` | 한 버전의 문항·검토 상태 조회 |
| POST | `/{testSetId}/versions` | 확정본에서 새 초안 생성 |

생성 Body:

```json
{"name":"취소 안내 검증","policyVersionIds":["정책 버전 UUID"]}
```

한 테스트셋 버전에 정책 버전 최대 20개, 문항 최대 200개를 저장합니다. 정책 식별자 policyId와 정책 버전 식별자 policyVersionId는 서로 다릅니다. 해당 프로젝트에 속한 버전만 참조할 수 있습니다.

## 문항 작성·검토·확정

기준 경로 `V = /api/projects/{projectId}/testsets/{testSetId}/versions/{versionId}`를 사용합니다.

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | `V/questions` | 수동 문항 추가. 초안만 가능 |
| GET | `V/questions/{questionId}` | 단일 문항과 검토 상태 조회 |
| PATCH | `V/questions/{questionId}` | 질문·기준·근거 전체 교체, 기존 승인은 pending으로 초기화 |
| DELETE | `V/questions/{questionId}` | 초안 문항 삭제. If-Match에 버전 revision 필요 |
| POST | `V/questions/{questionId}/review` | 사람이 approved 또는 rejected 판정 |
| POST | `V/confirm` | 문항이 하나 이상이며 전부 approved일 때만 확정 |
| GET | `V/evaluation-snapshot` | 확정된 버전의 고정 평가 입력 |

문항 생성·수정 Body:

```json
{
  "expectedRevision": 1,
  "question": "출발 30시간 전에 취소할 수 있나요?",
  "expectedBehavior": "취소할 수 있다고 안내한다.",
  "forbiddenBehaviors": ["정책에 없는 보상을 약속한다."],
  "policyVersionId": "정책 버전 UUID",
  "ruleKeys": ["cancellation-deadline"],
  "evidenceQuote": "예약 취소는 출발 24시간 전까지 가능합니다.",
  "category": "normal"
}
```

질문·기대 행동은 각 2,000자, 금지 행동은 최대 20개(각 2,000자), category는 normal/exception/boundary/ambiguous입니다. ruleKeys는 실제 규칙 key여야 하고, evidenceQuote는 지정 원문에 실제로 포함돼야 합니다. 문자열 일치만으로 의미상 올바른 판정 기준임을 보장하지 않으므로 사람이 반드시 검토합니다.

생성·수정·검토는 갱신된 버전 전체를 반환합니다. 문항 추가 시 Location은 단일 문항 URL입니다. expectedRevision은 **문항 revision이 아닌 테스트셋 버전 revision**입니다. 모든 변경 시 증가하며 오래된 revision은 409입니다. expectedRevision은 JSON 정수 1~2,147,483,647만 허용하고 문자열·소수·불리언·범위 밖 값은 400으로 거부합니다. 문항이 전부 승인돼도 confirm 전에는 평가할 수 없습니다.

검토 Body: `{"expectedRevision":2,"status":"approved","note":"원문과 일치함"}`

확정 Body: `{"expectedRevision":3}`

확정 후에는 편집·삭제·생성·재검토를 거부합니다. 수정하려면 `POST /testsets/{testSetId}/versions`에 `sourceVersionId`와 **테스트셋 메타데이터의 expectedRevision**을 보냅니다. 기존 문항을 복사하되 전부 pending으로 되돌립니다. 새 정책 버전을 사용하려면 `policyVersionIds`도 보내며, 이 경우 기존 문항을 복사하지 않고 빈 초안을 만듭니다.

## AI 질문 생성 연결 지점

`POST V/questions/generate`

```json
{"expectedRevision":1,"count":10}
```

Idempotency-Key 헤더가 필수입니다. 같은 사용자·버전·키·Body는 성공 후 24시간 원래 결과를 재사용합니다. Body가 다르면 409입니다. 재사용 응답은 당시 결과이므로 현재 상태가 필요하면 버전을 다시 조회하세요. 실패는 문항·revision·재사용 레코드를 남기지 않습니다. 한 번에 1~50개를 요청할 수 있습니다.

기업 챗봇 호출과 별개로 `app/services/bedrock_ai.py`의 Bedrock 어댑터가 정책 원문·규칙을 전송합니다. 도구 입력으로 받은 JSON을 다시 검증하며, 연결 5초·응답 읽기 55초·입력 512KiB·응답 2MiB로 제한합니다. 사용자당 호출 제한은 공통 AUTH_RATE_WINDOW_SECONDS(기본 60초)당 10회이며 실패와 재사용 요청도 포함합니다. 자동 재시도는 하지 않습니다. 생성 유형 선택·오류와 비용 관리 방법은 A 기능 인계를 참고하세요.

생성 결과는 개수·길이·스키마·정책 소속·규칙·근거 인용을 검사한 뒤 하나의 트랜잭션으로 저장합니다. 생성된 질문은 항상 pending입니다. 승인 또는 확정을 AI가 수행하지 않습니다.

## ④ 평가 담당자 연동

```python
from app.services.policy import get_evaluation_snapshot

# 먼저 OwnedProject로 사용자의 프로젝트 소유권을 검사합니다.
snapshot = get_evaluation_snapshot(db, project.id, testset_version_id)
```

snapshot은 schemaVersion, projectId, testSetId, testSetVersionId, version, confirmedBy, confirmedAt, policies, questions를 포함합니다. policies에는 당시 원문과 규칙, 새로 확정한 schemaVersion=2의 questions에는 기대 행동·금지 행동·expectedConditions·정책 버전·근거·검토자·검토 시각이 포함됩니다. 기존 schemaVersion=1 확정 스냅샷은 수정하지 않습니다. draft이면 409입니다. 평가 실행 시 이 JSON을 평가 입력으로 복사해 보존하고, 실행 중 최신 정책이나 초안 문항으로 다시 조회해 대체하지 마세요. 프롬프트·챗봇 연동 설정의 실행 당시 보존은 ②·④ 기능에서 추가합니다.

## 주요 예외와 검증

공통 오류 형식과 X-Request-Id를 사용합니다.

- 400: 입력 오류, INVALID_CURSOR, INVALID_POLICY_REFERENCE, INVALID_POLICY_EVIDENCE, QUESTION_LIMIT_EXCEEDED
- 404: RESOURCE_NOT_FOUND
- 409: REVISION_CONFLICT, REVIEW_REQUIRED, TESTSET_IMMUTABLE, TESTSET_NOT_CONFIRMED, IDEMPOTENCY_CONFLICT
- 412/428: If-Match 불일치/누락
- 502: INVALID_GENERATION_OUTPUT
- 503: GENERATION_NOT_CONFIGURED 또는 GENERATION_UNAVAILABLE
- 504: GENERATION_TIMEOUT

```bash
.venv/bin/python -m pytest -q
# 아래 DB는 반드시 전용 임시 테스트 DB여야 합니다.
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@127.0.0.1:3306/osori_test?charset=utf8mb4' .venv/bin/python -m pytest -q
.venv/bin/python -m alembic check
```

테스트의 가짜 생성기는 테스트에서만 의존성 주입으로 사용합니다. 서비스 기본값에는 연결되지 않습니다. 만료된 생성 재사용 레코드는 기존 `python -m app.maintenance` 명령으로 정리하며 자동 예약은 구성하지 않습니다.
