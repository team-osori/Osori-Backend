# 평가 실행·결과 비교 인계

같은 질문에 같은 연결 설정으로 A/B 프롬프트를 각각 전송하고, 같은 정책·판정 기준으로 결과를 비교합니다. 본인 프로젝트의 서비스만 접근할 수 있습니다. 제출 시 확정 테스트셋의 질문·기대 행동·금지 행동·검토자·정책 원문과 버전, A/B 프롬프트 원문과 버전, 연결 설정, 판정 기준 버전, 평가 모델 정보를 복사해 보존합니다. 이후 원본을 수정하거나 삭제해도 평가 입력은 바뀌지 않습니다.

## 기업 API에 권장하는 응답

아직 기업 API 계약은 확정되지 않았습니다. 아래 형식을 권장하며 외부 API 실연동 검증은 계약 확정 후 진행해야 합니다.

```json
{
  "answer": "출발 24시간 전까지 예약을 취소할 수 있습니다.",
  "retrieval": {
    "documents": [
      {
        "id": "refund-v2-section-3",
        "title": "예약 취소 정책",
        "text": "예약 취소는 출발 24시간 전까지 가능합니다.",
        "source": "policy/refund/v2"
      }
    ]
  },
  "citations": [
    {
      "documentId": "refund-v2-section-3",
      "quote": "예약 취소는 출발 24시간 전까지 가능합니다."
    }
  ]
}
```

- `answer`: 필수, 비어 있지 않은 답변 문자열(최대 100,000자).
- `documents`: 실제로 검색되어 답변 생성에 제공된 문서 또는 청크. 검색 내용을 요약해서 만들지 말고 원문을 반환합니다. `id`는 문서 버전·청크를 식별하며 동일 원문·버전에서는 유지합니다. 서로 다른 문서는 다른 ID를 씁니다. 각 항목의 `id`, `text`는 필수이고 `title`, `source`는 선택입니다. 최대 20개, text는 각각 10,000자까지 지원합니다.
- `citations`: 답변이 실제로 인용한 근거(최대 50개). `documentId`는 documents에 존재해야 하며 `quote`는 해당 문서 text에 그대로 포함되어야 합니다. 인용이 없으면 `[]`입니다.
- `documents: []`는 검색했지만 결과가 없는 경우입니다. 필드 생략 또는 `null`은 검색 정보를 제공하지 않은 경우입니다. 평가 결과에도 둘을 구분해 저장합니다.
- `source`는 출처 표시용입니다. Osori는 해당 주소에서 문서를 추가로 내려받지 않습니다.
- 길이 초과·중복 ID·존재하지 않는 문서 인용·원문에 없는 quote는 `INVALID_UPSTREAM_RESPONSE` 호출 실패입니다. 일부 문서를 몰래 잘라 평가하지 않습니다. 필요한 내용을 제공할 수 없다면 미제공으로 명시하세요.

연결 설정 PUT에 다음 선택 항목을 추가합니다. 기존 answer-only 설정도 동작합니다.

```json
{
  "answerPointer": "/answer",
  "retrievalPointer": "/retrieval/documents",
  "citationsPointer": "/citations"
}
```

필드 경로만 변경할 수 있으며 documents·citations 내부 구조는 위 계약을 따릅니다. 설정된 경로가 응답에서 빠지면 잘못된 응답으로 처리하므로, 미제공은 해당 위치에 `null`을 반환하거나 연결 설정에서 경로를 생략합니다. `citationsPointer`를 설정하려면 `retrievalPointer`도 있어야 하며, 문서 없는 인용 설정은 저장 시점에 400으로 거부합니다. 설정 변경은 연결 revision을 올리고 기존 연결 검증을 해제하므로 다시 연결 테스트를 진행하세요.

## 실행 준비

```bash
.venv/bin/python -m alembic upgrade head
# API와 별도 터미널; API와 같은 DATABASE_URL·CONNECTION_ENCRYPTION_KEY 사용
.venv/bin/python -m app.evaluation_worker
# 한 실행만 처리하고 종료할 때
.venv/bin/python -m app.evaluation_worker --once
```

마이그레이션 `20261105_07`이 평가 실행·문항 테이블을 추가합니다. 연결 테스트 워커 `app.connection_worker`도 별도로 실행해야 합니다. 평가 워커를 실행하지 않으면 제출한 작업은 QUEUED에 남습니다.

평가 실행 조건:

1. 현재 연결 revision으로 연결 테스트 성공(`verifiedRevision == revision`).
2. 연결 모드 `PROMPT_BODY`, 인증 방식에 따른 secret 등록 완료.
3. 같은 프롬프트의 서로 다른 A/B 버전 선택.
4. 사람이 검토·확정한 테스트셋 버전 선택(1~200문항).

## API

기본 경로: `/api/projects/{projectId}/services/{serviceId}/evaluations`. 모든 요청에 `Authorization: Bearer <accessToken>`을 사용합니다.

| 메서드·경로 | 용도 |
| --- | --- |
| POST 기본 경로 | 비동기 실행 제출, 202와 Location·Retry-After 반환 |
| GET 기본 경로 | 실행 목록, `limit`·`cursor` 페이지네이션 |
| GET `/{evaluationId}` | 상태·진행 문항 수·실패 문항 수·평가 모델 정보 |
| GET `/{evaluationId}/inputs` | 평가 당시 입력 스냅샷(인증정보 제외) |
| GET `/{evaluationId}/summary` | 기준별 통과율, 실패·판정 불가 건수, A/B 비교 |
| GET `/{evaluationId}/items` | 문항 목록과 개선·악화 분류, `limit`·`cursor` |
| GET `/{evaluationId}/items/{itemId}` | 질문·기대 행동, A/B 답변, 검색 문서·인용, 판정·근거·오류 |

제출 예시:

```http
POST /api/projects/{projectId}/services/{serviceId}/evaluations
Authorization: Bearer <accessToken>
Idempotency-Key: <클라이언트가 생성한 실행 요청 UUID>
Content-Type: application/json

{
  "connectionRevision": 2,
  "testSetVersionId": "<확정 테스트셋 버전 UUID>",
  "aPromptVersionId": "<기존 프롬프트 버전 UUID>",
  "bPromptVersionId": "<수정 프롬프트 버전 UUID>"
}
```

`Idempotency-Key`는 공백 없는 ASCII 1~100자입니다. 같은 사용자·서비스·키·요청은 같은 실행을 반환합니다. 같은 키에 다른 요청을 보내면 `409 IDEMPOTENCY_CONFLICT`입니다. QUEUED·RUNNING 키는 만료하지 않습니다. 종료한 실행은 생성 후 24시간이 지나면 같은 키로 새 실행을 제출할 수 있으며 기존 이력은 보존합니다. 명시적으로 새 평가를 실행할 때는 새 UUID를 사용하는 것이 간단합니다.

## 상태와 실패

실행은 `QUEUED → RUNNING → COMPLETED | PARTIAL_FAILED | FAILED`로 진행합니다. `completedQuestions`는 처리가 끝난 문항 수이며 실패·중단·미실행으로 종료한 문항도 포함합니다. `failedQuestions`는 그중 정상 완료되지 못한 문항 수입니다. 기업 API 호출 실패 건수는 summary의 `callFailed`로 확인하며 작업 중단·미호출과 구분합니다.

- `COMPLETED`: 모든 문항의 A/B 호출·처리가 끝났습니다. 정책을 모두 통과했다거나 AI 판정이 모두 가능하다는 의미는 아닙니다.
- `PARTIAL_FAILED`: 호출 실패·중단·미실행으로 끝난 문항과 저장된 성공 답변이 함께 있습니다.
- `FAILED`: 실패·중단·미실행으로 끝났으며 저장된 성공 답변이 없습니다. 실행 설정을 사용할 수 없는 경우도 포함합니다.
- 문항의 A/B에는 각각 `callStatus`, `answer`, `evidence`, `evidenceCatalog`, `latencyMs`, `attemptCount`, `error`, `judgeError`, `criteria`가 저장됩니다. A 호출 실패여도 B와 다음 문항은 계속 실행합니다.
- AI 호출·출력 검증 실패는 `judgeError`와 네 기준의 `status=error`, `verdict=null`로 표시하고 성공한 기업 답변은 보존합니다. 검색 문서가 없더라도 모델 오류를 판정 불가로 덮어쓰지 않습니다. 모델이 유효한 결과를 반환한 경우에만 검색 자료 미제공을 not_evaluable로 처리하며 기준 자체의 error 결과도 보존합니다.

DB 큐는 프로세스 재시작 후에도 유지합니다. 여러 워커가 실행을 중복 수행하지 않도록 원자적으로 선점하고 쓰기 전에 lease를 검사합니다. RUNNING에서 heartbeat가 10분 동안 갱신되지 않으면 `WORKER_INTERRUPTED`로 종료하며 이미 저장된 A/B 결과는 보존합니다. A 호출을 시작하기 전에 A=RUNNING·B=NOT_RUN을 함께 저장하고, B도 호출 전에 RUNNING을 저장합니다. 복구 시 QUEUED 문항과 명시적인 NOT_RUN 쪽은 미호출로, RUNNING 쪽은 결과 불명인 INTERRUPTED로 처리합니다. 호출 시작 기록이 없는 이전 구현의 RUNNING 결과는 보수적으로 INTERRUPTED로 구분합니다. 실제 외부 호출이 완료됐는지 모르는 작업은 자동으로 재전송하지 않습니다. 사용자가 새 요청 키로 다시 실행할 수 있습니다. SIGTERM/SIGINT를 받으면 현재 문항 처리를 마치고 나머지 문항은 `WORKER_STOPPED`로 종료합니다.

문항 상태는 `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `NOT_RUN`, `INTERRUPTED`입니다. NOT_RUN은 양쪽 모두 미호출인 채 종료한 문항, INTERRUPTED는 일부만 끝났거나 결과를 확인할 수 없는 문항입니다. A/B의 `callStatus`는 아래처럼 구분합니다.

| callStatus | 의미 | summary 집계 |
| --- | --- | --- |
| NOT_RUN | 호출하지 않음(작업 중단 이유는 error로 확인) | notRun |
| RUNNING | 호출 또는 판정 처리 중 | inProgress |
| SUCCEEDED | 답변 저장 완료 | 기준별 pass·fail·notEvaluable·judgeFailed |
| FAILED | 호출·응답 처리 실패가 확인됨 | callFailed |
| INTERRUPTED | 처리 도중 중단되어 결과를 확인할 수 없음 | callInterrupted |

RUNNING·INTERRUPTED의 `attemptCount`, `latencyMs`는 미확정이므로 null입니다. NOT_RUN은 각각 0이며 기업 API 오류로 집계하지 않습니다. 이미 저장된 A/B 성공·실패 결과는 복구 시 그대로 보존합니다.

기업 API 재시도는 기존 연결 설정의 `retrySafe=true`와 `maxRetries`에 따릅니다. API 내부 세션·쿠키는 A/B 사이에 공유하지 않습니다. 평가 시점의 인증정보는 암호화된 비공개 실행 필드에 임시 보존하고, 종료 시 지웁니다. API 응답·입력 조회로 암호문이나 평문을 노출하지 않습니다. 대기 중 인증정보를 변경해도 제출 당시 값을 사용합니다. 암호화 키를 바꾸거나 모델 어댑터 설정이 제출 시점과 달라지면 `EXECUTION_CONFIG_UNAVAILABLE`로 종료합니다.

## 판정 모델 연결

`EVALUATION_JUDGE_PROVIDER=bedrock`으로 서버 IAM 역할 기반 판정을 활성화합니다. 기본 모델은 Claude Haiku 4.5이며 API 키는 필요 없습니다. 이 문서는 기존 v1 네 기준을 설명하며 v2 조건별 판정은 [분석 인계](ANALYSIS_HANDOFF.md), 실제 모델·리전 설정은 [Bedrock 연동](BEDROCK_SETUP.md)을 참고하세요.

`app/services/evaluation_judge.py`의 `get_evaluation_judge()`에 선택한 제공사 어댑터를 연결하세요. API 프로세스와 평가 워커에 같은 구현·설정을 배포합니다. `descriptor()`에는 어댑터 및 판정 프롬프트 버전·제공사·모델·생성 설정 등 결과에 영향을 주는 비밀이 아닌 식별 정보를 담고, `judge()`는 저장된 질문·정책·기준·답변·검색 근거를 사용해 아래 네 기준을 모두 반환해야 합니다.

- retrieval: 질문에 필요한 정책 근거가 검색 문서에 있는가.
- grounding: 답변이 검색 문서와 인용 근거에 충실한가.
- response: 기대 행동에 맞는 명확한 답변인가.
- policy: 정책·금지 행동을 준수하는가.

모델은 `retrieval`, `grounding`, `response`, `policy` 네 키를 가진 객체를 반환합니다. 각 값은 사용자가 정한 아래 형식을 따릅니다. 이 예시는 `criteria.policy` 한 개의 결과입니다. 실제 question_id에는 요청받은 문항 UUID를 그대로 넣습니다.

```json
{
  "question_id": "q_001",
  "status": "judged",
  "verdict": "pass",
  "reason": "정책에 문의 답변 기한이 명시되어 있다.",
  "evidence_refs": [
    {
      "evidence_id": "ev_002",
      "explanation": "영업일 기준 3일이라는 구체적인 기한을 확인했다."
    }
  ],
  "error": null
}
```

| status | verdict | evidence_refs | error |
| --- | --- | --- | --- |
| judged | pass 또는 fail | 1개 이상, 중복 ID 불가 | null |
| not_evaluable | null | 빈 목록 가능 | null |
| error | null | 빈 목록 | code·message 객체 |

모든 상태에 비어 있지 않은 reason이 필요합니다. 어댑터는 `question_id`와 원문 근거의 ID를 생성하지 않습니다. 워커가 질문 ID와 함께 `evidence_catalog`를 제공하며 각 항목에는 `evidence_id`, `kind`, `source_id`, `text`가 있습니다. 이 ID들은 해당 실행·문항·A/B 한쪽의 범위에서 유효합니다. `ev_001`은 답변, 그 뒤에는 정책 버전과 검색 문서 순서로 ID를 부여합니다.

문항 상세의 `a.evidenceCatalog`·`b.evidenceCatalog`에는 text를 중복 저장하지 않고 ID·kind·source_id 연결을 제공합니다. kind=answer는 해당 쪽의 answer, kind=policy는 `/inputs`의 testSet.policies에서 source_id와 id가 같은 정책 버전의 content, kind=retrieval은 해당 쪽 evidence.documents에서 id가 source_id와 같은 문서의 text로 원문을 확인합니다. `explanation`은 모델의 해석이며 그 자체를 원문으로 취급하지 않습니다.

Bedrock 판정은 워커 120초(연결 5초·응답 읽기 110초), 기존 OpenAI 어댑터는 워커 30초 제한을 적용합니다. 워커는 네 기준의 출력 구조·상태와 verdict 조합·요청한 question_id 일치·근거 ID 존재를 검사합니다. 잘못된 출력은 `INVALID_JUDGE_OUTPUT`입니다. 근거 설명의 의미적 정확성은 선택한 모델과 실제 사례로 검증해야 합니다. 어댑터는 정책·질문·기업 답변을 신뢰할 수 없는 평가 자료로 취급하고, 그 안의 지시가 평가 기준이나 시스템 지시를 덮어쓰지 않도록 작성해야 합니다.

현재 기본 어댑터는 네 기준 모두 `status=not_evaluable`, `verdict=null`, `judgeError=JUDGE_NOT_CONFIGURED`를 반환합니다. 검색 정보가 미제공이면 모델이 연결되어도 retrieval·grounding은 `status=not_evaluable`, `verdict=null`입니다. `documents: []`는 자료가 없는 검색 결과 자체를 평가할 수 있습니다. 모델이 연결되기 전에 종료한 결과를 자동 재판정하지 않습니다. 모델 연결 후 새 실행을 제출하세요.

## 통과율과 비교

`통과율 = pass / (pass + fail) × 100`으로 계산하고 소수 둘째 자리까지 표시합니다. status가 not_evaluable·error인 판정, 확인된 호출 실패, 결과 불명 중단, 처리 중, 미호출은 분모에서 제외하며 각각 `notEvaluable`, `judgeFailed`, `callFailed`, `callInterrupted`, `inProgress`, `notRun`으로 제공합니다. 분모가 0이면 `passRate: null`입니다. 기준들을 임의로 합친 종합 점수는 만들지 않습니다. 진행 중 summary는 `provisional: true`입니다.

A/B 비교는 **같은 문항·같은 기준에서 양쪽 모두 status=judged이며 verdict가 pass/fail인 경우**만 사용합니다.

| A → B | 분류 |
| --- | --- |
| fail → pass | IMPROVED |
| pass → fail | WORSENED |
| pass → pass | UNCHANGED_PASS |
| fail → fail | UNCHANGED_FAIL |
| 한쪽이라도 미실행·실패·판정 불가 | NOT_COMPARABLE |

예: `/items?criterion=policy&change=IMPROVED`. change 필터에는 criterion이 필요합니다. comparison의 `pairedDenominator`, `aPairedPassRate`, `bPairedPassRate`로 같은 비교 가능 문항 집합의 통과율을 확인할 수 있습니다. A/B 개별 passRate는 판정 가능한 문항 집합이 다를 수 있으므로 이것만으로 개선을 단정하지 마세요.

질문·연결 설정·기준은 동일하게 고정하지만 기업 API가 프롬프트를 실제 적용했는지 검증할 계약은 아직 없고, 기업 내부 검색 결과·모델의 무작위성도 통제하지 않습니다. 따라서 `comparisonAssurance.promptAppliedVerified=false`, `retrievalInputsControlled=false`를 제공합니다. 현재 결과는 관측한 A/B 결과 비교이며 프롬프트만 바뀐 완전한 통제 실험을 보장하지 않습니다.

## 검증

```bash
.venv/bin/python -m pytest -q tests/test_evaluations.py tests/test_chatbot_gateway.py tests/test_migrations.py
# 반드시 별도 임시 MySQL DB 사용
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@127.0.0.1:3306/osori_test?charset=utf8mb4' \
  .venv/bin/python -m pytest -q tests/test_mysql_evaluations_integration.py
```

실제 기업 API·판정 모델을 사용하는 테스트가 아니라 격리된 전송·판정 대역으로 로직을 검증합니다. 실제 API와 모델이 정해지면 연결 테스트와 확정 테스트셋으로 새 평가를 실행해 확인해야 합니다.

## OpenAPI 계약 보완 (#19)

- `criteria` 응답은 현재 v2의 `policy/questionResolution/communicationQuality`와 과거 v1의 `retrieval/grounding/response/policy`를 모델 union으로 구분합니다. 항목의 점수·근거·오류와 비교 응답의 조건별 변경·집계·커서도 명세에 포함합니다. `passRate`는 0~100 백분율이며 판정 분모가 0이면 null입니다.
- 문항 목록의 `criterion`이 해당 평가 기준에 없으면 200과 빈 페이지를 반환합니다. `change`에는 `criterion`이 필요하며 SINGLE 평가는 `/comparison`으로 비교합니다.
- `/comparison`은 `baselineEvaluationId`를 생략하면 재실행 원본을 사용합니다. 원본도 없으면 `400 BASELINE_REQUIRED`입니다.
- `/reruns`는 종료된 SINGLE v2 평가만 지원합니다. 원본 질문·기준·모델·연동 설정 일치 제약은 유지합니다.
- 삭제 API의 `If-Match`는 따옴표를 포함한 revision(예: `"3"`)입니다. 생략은 428, 형식 또는 값 불일치는 기존처럼 412입니다. 패턴은 OpenAPI에 명시하고 서버의 기존 오류 계약은 보존합니다.
- `ConnectionPut.executionMode`는 `PROMPT_BODY/CONNECTION_ONLY` enum입니다. 연결 테스트의 `promptVersionId`는 PROMPT_BODY일 때 필수입니다.

CI에서 OpenAPI 구조와 실제 API 응답·필터 동작을 회귀 검사합니다(`tests/test_openapi_contract.py` 및 평가 API 테스트).

## 평가 중단·재개와 분석 지원 상태 (#20)

`POST …/evaluations/{evaluationId}/cancel`에 `{}`를 보내면 QUEUED/RUNNING 평가를 `CANCELLED`로 바꿉니다. 같은 중단은 멱등이며 종료된 다른 평가는 409입니다. 기존 결과는 보존하고 현재 호출 중인 항목은 INTERRUPTED, 미호출 항목은 NOT_RUN으로 구분합니다. 워커 lease를 바꿔 늦은 응답의 쓰기를 차단하고 실행 중 비동기 호출 대기를 취소합니다. **외부 기업 서버가 이미 받은 요청의 실행까지 취소됨을 보장하지는 않습니다.**

`POST …/evaluations/{evaluationId}/resume`에 `{"connectionRevision":N}`을 보내면 같은 평가에서 NOT_RUN인 측만 QUEUED로 재개합니다. 저장된 성공/실패나 INTERRUPTED 호출은 중복 실행하지 않습니다. 남은 미호출 항목이 없으면 `409 NO_PENDING_QUESTIONS`, 연동 revision·설정·판정 모델이 달라지면 `409 COMPARISON_CONFIGURATION_CHANGED`입니다. 재개한 마지막 항목까지 처리한 후 전체 결과에 따라 COMPLETED/PARTIAL_FAILED/FAILED로 끝납니다. 원문 스냅샷과 기존 사람 검토는 유지합니다.

`GET …/evaluations/capabilities`는 각 분석 기능의 `supported/reason`을 반환합니다. 현재는 근거가 있을 때 원인 **가설**을 지원하며, 확정 원인 분류·프롬프트 수정안/diff·독립 채점자 일치도·반복 실행 불안정성·수정 효과 예측 개수는 미지원으로 명시합니다. 프론트는 미지원 영역을 숨기거나 사유를 표시하고 임의 숫자를 채우지 않습니다. 이는 프로젝트별 멤버 구현과 함께 합의한 현재 범위입니다.
