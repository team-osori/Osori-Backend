# A 담당: 정책·질문·기대 조건·검토 이력

기업 정책에 어긋나거나 근거가 부족한 챗봇 답변을 찾기 위한 평가 입력을 제공합니다. 기존 정책·테스트셋 API를 확장했으며 인증·챗봇 연결·평가 실행 API는 유지합니다. 화면을 분리할 필요 없이 문항 상세에서 수정·검토·이력을 함께 사용할 수 있습니다.

## 실행 설정

`alembic upgrade head`로 `20261105_08`을 적용합니다. 기존 질문에 nullable 검토 시각을 추가하고 `question_history`를 생성합니다. 기존 확정 스냅샷과 평가 결과는 수정하지 않습니다.

```dotenv
QUESTION_GENERATION_PROVIDER=openai
QUESTION_GENERATION_MODEL=gpt-4.1-mini-2025-04-14
```

키는 Git에서 제외된 환경 파일의 `QUESTION_GENERATION_API_KEY`에 둡니다. 로컬에서는 `OPENAI_API_KEY`도 지원하며 전용 키가 우선합니다. 제공사 설정이 비어 있으면 키가 있어도 호출하지 않습니다. 모델은 환경 변수로 교체할 수 있습니다. 테스트는 provider를 비활성화하고 전송 대역을 사용합니다.

## 질문 생성

기존 경로 `V = /api/projects/{projectId}/testsets/{testSetId}/versions/{versionId}`를 사용합니다.

`POST V/questions/generate`, `Idempotency-Key` 필수:

```json
{
  "expectedRevision": 1,
  "count": 3,
  "categories": ["normal", "exception", "boundary"]
}
```

- `categories`는 선택입니다. 생략하면 실제 OpenAI 어댑터는 count=1일 때 normal, 2일 때 normal·exception, 3 이상은 normal·exception·boundary를 포함합니다.
- 명시한 유형은 중복 없이 각각 최소 1문항 생성합니다. ambiguous도 선택할 수 있습니다. count는 유형 수 이상, 최대 50입니다.
- 경계값은 기한·수량·자격 등 정책 적용 경계입니다. 정책에 없는 예외나 숫자는 발명하지 않도록 지시하며, 생성 후 사람이 의미를 검토해야 합니다.
- 생성된 문항은 항상 pending입니다. 원문에 없는 인용, 잘못된 정책·규칙, 누락된 유형, 중복 질문, 잘못된 조건은 저장하지 않습니다. 실패하면 revision·질문·이력·재사용 레코드가 모두 롤백됩니다.
- 사용자당 공통 `AUTH_RATE_WINDOW_SECONDS`(기본 60초)당 최대 10회입니다. 실패·재사용 요청도 요청 횟수에 포함합니다. 한 번의 생성은 입력 512KiB, 응답 2MiB, 총 60초로 제한하고 자동 재시도하지 않습니다. 대량 생성이 시간/출력 제한에 걸리면 문항 수를 줄여 새 요청을 보냅니다.
- 정책 원문을 OpenAI에 전송하며 `store=false`를 설정합니다. 입력 문서 안의 지시를 실행하지 않도록 지시하고 출력은 정책 원문과 다시 대조합니다. 문자열 일치 검사는 의미적 타당성을 보장하지 않습니다.

주요 오류: `GENERATION_NOT_CONFIGURED`(503), `GENERATION_AUTH_FAILED`(503), `GENERATION_RATE_LIMITED`(503), `GENERATION_UNAVAILABLE`(503), `GENERATION_TIMEOUT`(504), `GENERATION_INCOMPLETE`(502), `GENERATION_REFUSED`(502), `GENERATION_SCHEMA_INVALID`(502), `GENERATION_COUNT_MISMATCH`(502), `GENERATION_CATEGORY_MISMATCH`(502), `GENERATION_EVIDENCE_INVALID`(502), `GENERATION_CONDITIONS_INVALID`(502), `GENERATION_DUPLICATE_QUESTIONS`(502), `GENERATION_INPUT_TOO_LARGE`(400), 사용자 요청 제한 `RATE_LIMITED`(429, Retry-After).

## 기대 조건

기존 질문 생성·수정 Body에 `expectedConditions`를 선택적으로 추가합니다. 질문 자체의 기존 `question`, `expectedBehavior`, `policyVersionId`, `ruleKeys`, `evidenceQuote` 필드는 계속 필요합니다.

```json
{
  "expectedConditions": [
    {
      "description": "출발 24시간 전까지 일반 예약을 취소할 수 있다고 안내한다.",
      "kind": "required",
      "category": "policy",
      "policyVersionId": "정책 버전 UUID",
      "ruleKeys": ["cancellation"],
      "evidenceQuote": "일반 예약은 출발 24시간 전까지 취소할 수 있습니다."
    }
  ],
  "changeNote": "경계값에서의 기대 조건을 구체화함"
}
```

- 최대 30조건, required 최소 1개. kind는 required/forbidden, category는 policy/response/grounding/retrieval입니다. **category는 표시용 분류이며 조건이 판정 단위입니다.**
- 조건별 정책 버전은 테스트셋에 포함되어야 하고 실제 규칙 key와 원문 인용이어야 합니다. 질문과 다른 정책을 조건의 근거로 참조하는 것도 테스트셋 내에서는 가능합니다.
- 새 조건의 `id`는 생략하거나 null로 보냅니다. 서버가 부여합니다. 수정 시 같은 문항의 기존 조건 ID만 재사용할 수 있습니다. 다른 문항의 ID, 임의 ID, 중복 ID는 400입니다.
- 명시한 조건을 기준으로 required 설명을 줄바꿈으로 합쳐 `expectedBehavior`를 저장하고 forbidden 설명을 `forbiddenBehaviors`에 저장합니다. 합친 기대 행동은 최대 2,000자, forbidden은 최대 20개입니다. B의 기존 요약 필드 소비와 충돌하지 않습니다.
- 기존 형식으로 조건 없이 요청해도 서버가 기대 행동·금지 행동을 조건으로 변환합니다. 기존 초안은 읽을 때 안정적인 ID를 제공하며 기존 확정 스냅샷을 다시 쓰지 않습니다. 기존 클라이언트의 전체 교체 요청은 기존 의미를 정확히 표현하지 못할 수 있으므로 새 클라이언트는 expectedConditions를 함께 전송하세요.
- 수정 후 기존 승인은 pending으로 돌아갑니다. 새 확정 버전에서 복사된 문항은 새 질문 ID를 받고 조건 ID는 유지하지만 다시 검토해야 합니다.

## 검토·수정 이력

- 기존 문항 상세에 `reviewedAt`(UTC ISO 8601, 미검토 null)을 추가했습니다. 과거에 기록하지 않은 검토 시각은 추정하지 않습니다.
- `GET V/questions/{questionId}/history`: 해당 문항의 생성·AI 생성·복사·수정·검토·삭제 이력. 삭제된 문항의 이력도 조회할 수 있습니다.
- `GET V/history`: 삭제된 문항을 포함한 전체 이력.
- 두 API 모두 limit/cursor 페이지네이션이며 버전 revision 내림차순입니다. 이력 항목은 id, questionId, versionRevision, action, actorId, note, before, after, createdAt을 포함합니다.
- POST/PATCH의 `changeNote`, 검토 요청의 `note`가 이유로 저장됩니다. 삭제 이벤트의 after는 null입니다. 이력과 본 변경은 같은 트랜잭션입니다. 이전 기능에서 보존하지 않은 과거 이력은 생성하지 않습니다.
- 검토 요청은 기존 API 그대로이며 별도 화면 이동이 필요 없습니다. 기존 confirm은 검토된 입력을 고정하기 위한 서버 작업으로 유지합니다.

## B 담당과 연결

`get_evaluation_snapshot(db, project.id, testset_version_id)`를 그대로 사용합니다. 새 확정본은 `schemaVersion=2`이며 questions에 조건과 reviewedAt이 추가됩니다. 과거 `schemaVersion=1`은 그대로 반환하므로 B는 두 버전을 수용해야 합니다.

1. 판정은 `testSetVersionId + question.id + expectedConditions[].id` 단위로 저장합니다. category 단위 점수만으로 개별 조건 판정을 대체하지 않습니다.
2. 조건의 description·kind·정책 버전·인용과 답변의 문제 문장을 연결합니다.
3. 재평가에서는 같은 확정 스냅샷을 사용합니다. 새 초안은 같은 조건 ID를 포함할 수 있어도 다른 테스트셋 버전이므로 자동으로 동일 기준 비교라고 간주하지 않습니다.
4. A가 제공하는 정책 근거는 기업 챗봇의 실제 검색 로그가 아닙니다. 검색·프롬프트·모델 원인 확정에는 B가 확보한 별도 실행 증거가 필요합니다.
5. 단일 평가·조건별 판정·담당자 결과 판정·해결/잔존/신규 비교는 B의 담당 범위입니다. 이 PR은 기존 A/B 실행과 판정 모델을 변경하지 않습니다.

## 검증

```bash
.venv/bin/python -m pytest -q
# 전용 임시 MySQL DB를 지정한 뒤 실행
.venv/bin/python -m pytest -q tests/test_mysql_question_conditions_integration.py
```

API 흐름·조건 출처·ID 검증·이력 보존·동시 수정·확정본 불변성·생성 오류/할당량·키 설정 전달을 검증합니다. 실제 모델 호출은 별도 가상 정책 3문항(normal/exception/boundary)으로 확인하며 일반 테스트에서는 외부 API를 호출하지 않습니다.

참고: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [사용 모델](https://developers.openai.com/api/docs/models/gpt-4.1-mini).

### 생성 실패 진단 (#17)

502는 형식, 개수, 유형, 정책 근거, 기대 조건, 중복 질문으로 구분합니다. 모델 원문·정책 내용·키는 오류에 넣지 않습니다. 실패 시 문항·이력·revision·멱등 응답은 저장하지 않습니다. `count < len(categories)` 또는 중복 유형은 모델 호출 전에 400입니다.

생성된 `evidenceQuote`는 공백·탭·줄바꿈의 차이만 허용하며 저장 시 실제 정책 원문의 연속 구간으로 복원합니다. 숫자·문장부호·단어·규칙 key·정책 버전은 완화하지 않습니다. 수동 입력과 정책 `sourceQuote`는 그대로 원문 일치를 요구합니다.

실패한 호출도 분당 10회 제한에 포함합니다. 제공사에서 비용이 발생한 실패를 무제한 재시도하지 않도록 기존 상한을 유지하며, 429의 `Retry-After` 이후 재시도합니다. 실제 배포 실패의 원인은 새 오류 코드로 확인해야 하며, 로컬 테스트는 제공사 대역만 사용했습니다.
