# 프롬프트 수정 제안 입력 계약 (#30·31)

입력 계약 최초 기준: origin/main `a68eba7`. PR 전 `05ba5b3`(#25 병합)을 반영했다. 별도 `prompt-suggestions` 모듈이 기존
`evaluation_analysis.item_analysis`와 `latest_reviews`를 사용한다.
평가·시험지·CSV·오답 분석의 저장 형식은 변경하지 않는다.

## 현재 main에서 요구하는 입력

- 종료된 SINGLE v2 평가, 명시적으로 선택한 `inputs.prompts.a.id/content/promptId`.
- `EvaluationItem.id/question_id/input/a/review_revision` 및 최신 사람 검토.
- `analysis.a.conditions`: conditionId, requirement, kind, category, criterion,
  policyEvidence(policyVersionId/quote), judgment(status/verdict/reason/evidence_refs).
- `analysis.a.issues`, `facts`, `hypotheses`, `limitations`는 관측 사실과 가설로 구분해 보존.
- 성공한 답변에 연결된 `judged/fail` 조건만 수정 근거로 채택한다.
  호출 오류·판정 오류·판정 불가는 오답이나 개선 가능 문항으로 세지 않는다.
- 정책 인용은 실행 당시 정책 원문, 답변 인용은 실행 당시 답변에서 확인한다.
  원본 평가의 프롬프트 버전과 요청한 baseVersionId가 같아야 한다.
- 생성 시점의 분석·검토 revision을 보존한다. 이후 검토가 바뀌면 적용을 거절하고
  새 근거로 다시 생성하도록 안내한다.

## 추정 계약

`expectedImprovedQuestionCount = null`, `method = NOT_ESTIMABLE`.
관측 오답과 수정안의 근거 연결만으로 교정 성공 확률을 추정할 검증 자료가 없다.
`targetedQuestionCount`는 선택한 수정안에 연결된 서로 다른 questionId의 수다.
이는 예측값·최대 개선 보장·신뢰구간이 아니다. 여러 조건/제안에 중복 연결된
문항을 한 번만 센다. 실제 효과는 별도 재시험과 comparison 응답으로만 표시한다.
검색·외부 모델·프롬프트 실제 적용 여부는 통제/검증되지 않았으므로 인과 효과로
단정하지 않는다. 모델에게 개선 개수나 확률 생성을 요청하지 않는다.

## 다른 작업과 최종 통합 검증

- #27: 확정 시험지의 question/condition ID, 정책 버전·원문 연결이 유지되는지 확인.
- #28·29: 위 conditions/analysis 입력 계약, 사람 검토 우선순위, 오류·판정 불가 구분,
  `review_revision` 갱신을 확인. 분석 응답 확장은 이 모듈의 근거 어댑터에서 수용한다.
- #28·29: comparison의 RESOLVED/NEW/NOT_COMPARABLE는 **조건** 단위다.
  수정 제안의 targetedQuestionCount(**문항** 단위)와 직접 차감하지 않는다.
- 각 브랜치 병합 후 동일 스냅샷 → 제안 → 버전 저장 → reruns → comparison의
  통합 테스트와 Alembic 단일 head를 다시 확인한다.
- #24·25 생성 작업은 수정하지 않는다. 현재 Bedrock Converse 전송·IAM 역할·
  QUESTION_GENERATION_PROVIDER/MODEL 및 AWS 리전 설정만 재사용한다.
  새 서비스·API 키를 도입하지 않으며 테스트에서 실제 모델 호출을 실행하지 않는다.

## 프론트 연동 순서

기본 경로 `S = /api/projects/{projectId}/services/{serviceId}`.
모든 경로에 기존 Bearer 인증과 프로젝트 멤버 권한을 적용한다. 다른 프로젝트·서비스의
평가/제안/버전은 404다. 신규 OpenAPI 태그는 `prompt-suggestions`다.

| 요청 | 응답과 사용법 |
| --- | --- |
| `GET S/prompt-suggestions/capabilities` | 현재 생성 제공자와 지원 상태. 기존 evaluation capabilities는 다른 작업과의 충돌을 피하기 위해 그대로 유지하며, 새 수정 제안 화면은 이 경로를 사용한다. |
| `POST S/prompt-suggestions` | 생성 요청. 필수 `Idempotency-Key`, body는 아래 예시. 최초 201, 같은 요청 재전송 200, 이미 생성 중이면 202 + Retry-After. |
| `GET S/prompt-suggestions` | 목록 `limit`(1~100, 기본 20)/`cursor`, 원문을 제외한 요약. |
| `GET S/prompt-suggestions/{id}` | 근거 스냅샷, 수정안, 선택 상태, 추정 한계, 적용 버전, 다음 API 링크. |
| `PATCH S/prompt-suggestions/{id}` | `expectedRevision`, `selectedSuggestionIds`. 빈 배열은 선택 해제. 전체 선택 상태 교체. |
| 기존 `POST S/prompts/{promptId}/versions` | 기존 필드에 `suggestionId`, `suggestionRevision`을 함께 추가하여 적용. |
| 기존 `POST S/evaluations/{baselineId}/reruns` | 새 버전 ID와 현재 connectionRevision으로 재시험. 별도 Idempotency-Key 필수. |
| `GET S/prompt-suggestions/{id}/results` | 원본 평가와 적용 버전이 일치하는 재시험 목록, 상태, comparisonUrl. 페이지네이션 지원. |
| 기존 `GET S/evaluations/{rerunId}/comparison` | 실제 개선/악화/비교 불가 결과. 원본 평가를 자동 기준으로 사용. |

### 1. 생성

```json
{
  "evaluationId": "원본-평가-ID",
  "baseVersionId": "평가에-사용한-프롬프트-버전-ID",
  "itemIds": ["오답-평가문항-ID"]
}
```

`itemIds`는 questionId가 아닌 평가 문항 ID다. 중복 없이 1~20개이며 각 문항에
성공한 답변과 하나 이상의 최신 `judged/fail` 조건이 필요하다. 프롬프트를 지정하지 않은
SINGLE, COMPARE, v1, 진행 중 또는 CANCELLED 평가는 이 흐름에서 지원하지 않는다.
스냅샷은 500KiB 이하로 제한한다. 현재 버전이 아닌 과거 평가 버전도 조회/생성할 수
있지만 최신 프롬프트가 이미 바뀌었다면 적용 시 충돌한다.

응답 `status`는 `GENERATING → READY | FAILED`, 적용 후 `APPLIED`다.
생성 호출은 동기식이며 기존 Bedrock 읽기 제한 55초, 연결 제한 5초, 자동 재시도 없음.
중복 요청은 먼저 기록한 리소스를 돌려준다. 같은 사용자·서비스·키에 다른 body를
보내면 `409 IDEMPOTENCY_CONFLICT`다. 키는 이력 보존 동안 만료하지 않는다.
**201이어도 status=FAILED일 수 있다.** 이는 실패 내역을 포함한 요청 리소스 생성 성공이며,
프론트는 항상 status와 error를 확인한다. 실패한 동일 키는 모델을 다시 호출하지 않는다.
재시도는 사용자가 명시적으로 새 키로 요청한다. 생성 중 프로세스가 중단되면 GET 또는
동일 키 재요청에서 5분 이후 `SUGGESTION_INTERRUPTED`로 종료한다. 외부 모델의 실제
처리가 끝났는지는 알 수 없으며 자동 재호출하지 않는다.

`snapshot.content`는 평가 때 프롬프트 원문이며 `snapshot.evidence`에는 서버가 부여한
`id`, itemId/questionId, 질문, 답변, 조건·판정 이유·정책 인용, reviewRevision,
관측 facts와 hypotheses/limitations가 보존된다. `snapshot.generator`는 제공자·모델·
리전·지시문 해시를 기록한다. 비밀이나 기업 API 인증정보는 포함하지 않는다.

각 `suggestions[]` 항목은 `id`, `originalText`, `replacementText`, `reason`,
`evidenceIds`, `start`, `end`를 포함한다. `reason`은 모델이 작성한 **검토 대상 설명**이다.
서버는 원문 인용의 유일성·존재, 근거 ID 범위, 결과 길이를 검증하며 의미적 타당성은
보장하지 않는다. 정책·질문·답변 내부의 지시는 모델 시스템 지시로 취급하지 않는다.

`[start,end)`는 **Unicode 코드 포인트** 인덱스다. JavaScript 문자열의 UTF-16 인덱스와
다를 수 있으므로 `Array.from(content).slice(start,end).join('')`로 원문을 강조한다.
프론트가 수정 문자열을 합성할 필요 없이 서버의 `previewContent`를 사용한다.

### 2. 선택과 적용

```json
{"expectedRevision": 1, "selectedSuggestionIds": ["수정안-ID"]}
```

서로 겹치는 수정은 대안이므로 함께 선택하면 409다. 선택 후 응답의 revision과
previewContent를 그대로 기존 버전 저장 API에 전달한다.

```json
{
  "baseVersionId": "원본-버전-ID",
  "content": "선택-응답의-previewContent-전체",
  "suggestionId": "제안-리소스-ID",
  "suggestionRevision": 2,
  "label": "오답 근거 반영",
  "changeNote": "선택한 취소 기한 수정 적용"
}
```

기존 버전 응답 형식은 유지된다. `data.id`가 **새 promptVersionId**이며 제안 상세의
`appliedPromptVersionId`에도 같은 ID가 저장된다. 버전 삽입과 제안 APPLIED 상태는
한 트랜잭션으로 처리한다. 같은 적용 body는 같은 버전을 다시 반환(201)하며 다른
내용의 재적용은 409다. 일반 버전 저장에는 신규 필드가 필요 없다.

선택 변경은 revision을 증가시키며 낡은 revision, 동시 선택/적용, 최신 프롬프트 변경,
근거/사람 검토 변경을 거절한다. 임의 편집한 content를 suggestionId와 함께 보낼 수 없다.
직접 편집은 기존 일반 버전 저장으로 할 수 있으나 그 버전을 제안 적용으로 표시하지 않는다.

### 3. 명시적 재시험과 실제 비교

```json
{"connectionRevision": 2, "promptVersionId": "버전-저장-응답의-id"}
```

이를 제안 응답 `links.rerun`으로 전송한다. 수정안 적용만으로 유료 재시험은 시작되지 않는다.
기존 reruns는 원본 시험지/정책/기대 조건/채점 프로파일 스냅샷을 복사하며 새 promptVersionId를
주입한다. 현재 연결 revision 검증·연결 테스트 성공·PROMPT_BODY 지원·판정 모델/연결 설정
동일성·200문항 제한을 그대로 검사한다. 변경된 현재 시험지로 대체하지 않는다.

`links.results`로 찾은 각 평가의 `comparisonUrl`을 조회한다.
진행 중에는 comparisonAvailable=false이고 기존 comparison API도 409를 반환한다.
새 키로 여러 번 재시험하면 각각의 결과가 표시된다. 본 모듈은 자체 비교 점수를 만들지 않는다.

- 조건별 `RESOLVED`: fail→pass, `NEW`: pass→fail, `REMAINING`: fail→fail.
- `NOT_COMPARABLE`: 미실행/호출 오류/판정 오류/판정 불가 등으로 비교할 수 없음.
- 전체 `MIXED`는 개선과 새 오류가 함께 있음, `REGRESSED`는 새 오류,
  `INCOMPLETE`는 비교 불가가 있음. `IMPROVED`도 외부 환경을 통제한 인과 효과가 아니다.
- comparison은 조회 시점의 최신 사람 검토를 반영한다. 반면 생성 당시 근거 스냅샷과
  estimate는 재시험 후에도 변경되지 않는다.

## 주요 오류와 UI 처리

| 코드 | 처리 |
| --- | --- |
| `INVALID_SUGGESTION_SOURCE`, `SUGGESTION_PROMPT_MISMATCH` | 종료된 SINGLE v2 평가와 실제 사용한 원문 버전을 선택 |
| `NO_FAILED_CONDITION`, `INVALID_SUGGESTION_EVIDENCE` | 관측 가능한 오답과 정책 근거 확인 |
| `SUGGESTION_INPUT_TOO_LARGE` | 선택 문항/원문 범위 축소 |
| `SUGGESTION_NOT_CONFIGURED` | 기존 Bedrock QUESTION_GENERATION_PROVIDER/MODEL, AWS 리전/IAM 설정 확인 |
| `SUGGESTION_TIMEOUT/AUTH_FAILED/REFUSED/BUSY` 등 | FAILED 리소스에 안전한 오류 코드 제공. 원문 AWS 오류는 노출하지 않음 |
| `INVALID_SUGGESTION_OUTPUT` | 수정안은 제공하지 않음. 새 키 재시도 여부를 사용자에게 맡김 |
| `INVALID_SUGGESTION_SELECTION`, `OVERLAPPING_SUGGESTIONS` | 유효한 수정안 선택, 겹치는 대안 중 하나 선택 |
| `SUGGESTION_CONFLICT` | 최신 상세/revision 조회 |
| `SUGGESTION_SELECTION_REQUIRED`, `SUGGESTION_CONTENT_MISMATCH` | 선택 완료 후 previewContent 사용 |
| `SUGGESTION_EVIDENCE_CHANGED` | 최신 사람 검토/분석으로 새 제안 생성 (미충족 조건이 사라지면 NO_FAILED_CONDITION) |
| `PROMPT_VERSION_CONFLICT` | 최신 프롬프트로 평가 후 새 제안 생성. 강제 덮어쓰기하지 않음 |
| `SUGGESTION_ALREADY_APPLIED` | 기존 적용 버전 확인 |
| 기존 reruns/comparison 오류 | 기존 EVALUATION_HANDOFF 계약대로 처리 |

## 마이그레이션·검증

전용 revision `20261105_3031`, 부모 `20261105_25`. 기존 평가/버전 테이블은 변경하지 않는다.
프로젝트 영구 삭제 시 evaluation FK의 cascade가 제안을 먼저 제거하므로 기존 버전 정리가
막히지 않는다. 병합 직전 최신 main head가 변경되면 down_revision을 다시 연결해야 한다.

```bash
python -m pytest -q tests/test_prompt_suggestions.py tests/test_connections_prompts.py tests/test_migrations.py
# OSORI_TEST_MYSQL_URL은 반드시 일회용 테스트 DB를 가리켜야 한다.
python -m pytest -q tests/test_mysql_prompt_suggestions_integration.py
python -m alembic heads
```

통합 테스트는 모델/기업 전송 대역만 사용한다. SQLite 전체 회귀와 MySQL 실제 트랜잭션에서
동일 키 동시 생성(모델 1회), 동일 적용 중복(버전 1개), 서로 다른 제안 동시 적용,
선택/적용 경합, JSON 재시험 연결을 검증한다. 실제 모델의 수정 품질, AWS 호출 권한,
기업 서버의 프롬프트 실제 적용 여부는 유료 호출 없이 검증할 수 없으므로 배포 후 별도의
승인된 소량 실험으로 확인해야 한다. 이 PR은 병합/배포하지 않는다.
