# #27 시험지·CSV 연동 계약

## 변경 전 합의 기준

- `expectedConditions`의 ID, kind(required/forbidden), category, 정책 근거 구조는 유지한다. 평가·프롬프트 API는 변경하지 않는다.
- 확정 스냅샷은 `schemaVersion: 2`를 유지한다. 확정된 문항과 모든 조건은 기존과 동일하게 유효한 정책 버전·규칙·인용을 가진다. 기존 저장 스냅샷을 재작성하지 않는다.
- 초안 문항의 최상위 근거는 `policyVersionId: null`, `ruleKeys: []`, `evidenceQuote: null`로 미연결을 표현한다. 세 필드는 한 번에 연결한다. 미연결 문항은 `expectedConditions: null`이며 승인·확정할 수 없다. 근거 연결은 기존 문항 PATCH를 사용한다.
- 신규 유형은 `normal`(정상), `ambiguous`(모호), `missing_document`(문서 없음), `policy_trap`(정책 함정). 기존 `exception`, `boundary`도 계속 허용한다. 문서 없음은 질문의 답을 문서에서 찾을 수 없다는 뜻이며, 승인하려면 추측 금지·확인 요청 등 기대 행동을 뒷받침하는 실제 정책 근거를 연결해야 한다. 원문 부재 자체를 인용 근거로 꾸미지 않는다.
- `modelAnswer`는 사람이 검토하는 예시 답안이다. `expectedConditions`는 평가 판정 기준이다. 명시된 조건이 있으면 기존처럼 required 설명을 `expectedBehavior`로 합치고 forbidden 설명을 `forbiddenBehaviors`로 투영한다. 예시 답안 변경이 명시 조건을 덮어쓰지 않는다. 조건을 생략한 기존 입력은 기대 행동/금지 행동에서 조건을 만든다. 기대 행동 없이 모범 답안만 입력하면 초기 기대 행동으로 사용하며, 사람이 조건을 다듬어 승인한다.
- 버전 상세의 `questionCount`, `approvedCount`는 현재 문항 수와 사람 승인 문항 수다. 모든 문항 승인 후 확정한다. 생성·가져오기는 pending, 수정은 승인을 무효화한다.
- 생성은 기존 `count`/`categories`를 유지하며 `categoryCounts`를 추가한다. 0인 유형은 생성하지 않고 합계는 1~50, 명시한 count는 합계와 같아야 한다. categories와 categoryCounts를 함께 보내지 않는다. 공급자 결과도 유형별 수를 검증한다.
- CSV 가져오기는 기존 버전 아래 `POST .../questions/import` multipart(file, expectedRevision)로 제공한다. UTF-8(BOM 허용), 헤더 `question,category,modelAnswer` 또는 `질문,유형,모범 답안`을 사용한다. 전체 검증 후 원자적으로 저장한다. 한 행이라도 실패하면 문항·revision·이력을 바꾸지 않고 행별 오류를 반환한다.

## 다른 작업에 전달할 계약

#28·29: 조건 구조와 v2 확정 스냅샷의 근거 불변식은 유지된다. 신규 문항 유형은 표시용이며 조건 category와 별개다. 선택적 `modelAnswer`를 추가로 받을 수 있으나 판정 기준은 기존 expectedConditions다.

#30·31: 확정본 기반 처리 방식은 유지한다. nullable 근거는 초안에만 존재한다. 기존 condition ID 보존과 수정 시 재승인 규칙을 재사용한다.

DB는 기존 JSON payload를 사용하므로 컬럼·마이그레이션 추가가 없다.

## 프론트엔드 호출 순서

`base = /api/projects/{projectId}/testsets/{testSetId}/versions/{versionId}`.
모든 요청에 `Authorization: Bearer ...`를 보내고, 응답은 기존 `{ "data": ... }` / `{ "error": ... }` 형식이다.

1. 기존 `POST /api/projects/{projectId}/testsets`로 정책 버전 1~20개를 지정해 초안을 만든다. 문서 없음 유형도 이 정책 범위를 기준으로 한다.
2. 아래 생성 또는 CSV 가져오기 요청으로 문항을 추가한다. 최대 버전당 200문항이다.
3. `GET {base}`의 `questions`, `questionCount`, `approvedCount`로 검토 화면을 표시한다. 이 개수는 버전 상세에 제공되며 버전 목록 요약에는 없다.
4. `PATCH {base}/questions/{questionId}`로 질문·답안·근거·조건을 수정한다. 기존과 같은 **전체 문항 교체** 요청이므로 유지할 모든 문항 필드를 보내야 한다. 응답 전용 id/reviewStatus/reviewedBy/reviewedAt/reviewNote는 보내지 않는다. 저장하면 pending으로 돌아간다.
5. `POST {base}/questions/{questionId}/review`에 `{ "expectedRevision": 4, "status": "approved" }`를 보내 승인한다. 거절은 `rejected`, 설명은 선택적 `note`다. 미연결 문항도 거절할 수 있다.
6. `POST {base}/confirm`에 최신 expectedRevision을 보낸다. 1개 이상의 모든 문항이 승인되어 있고 근거가 유효해야 한다. 확정된 문항은 추가·수정·삭제·가져오기할 수 없다. 수정하려면 기존 `POST /testsets/{testSetId}/versions`로 확정본을 복사하고 재승인한다.

문항 삭제는 `DELETE {base}/questions/{questionId}`, `If-Match: "5"`처럼 버전 revision을 따옴표로 감싼다. 삭제 응답은 204이므로 버전을 다시 조회한다. 그 외 쓰기 응답의 revision으로 화면 상태를 교체한다. revision은 문항별이 아닌 **버전 전체** 기준이며 한 CSV/생성 배치가 한 번 증가시킨다.

### 유형별 생성

```http
POST {base}/questions/generate
Content-Type: application/json
Idempotency-Key: client-generated-unique-key
```

```json
{
  "expectedRevision": 1,
  "categoryCounts": {
    "normal": 4,
    "ambiguous": 2,
    "missing_document": 2,
    "policy_trap": 2
  }
}
```

200 응답은 버전 상세다. count를 생략하면 합계로 정한다. 명시하면 합계와 같아야 한다. 각 값은 0~50 정수이며 boolean·음수·알 수 없는 유형은 400이다. 합계는 1~50이다. 기존 `count`/`categories` 호출과 기본 유형 분포(normal/exception/boundary)는 그대로 지원한다. 신규 네 유형을 원하면 categoryCounts를 명시한다.

유형별 개수는 OpenAI/Bedrock 요청과 응답 검증에 모두 적용된다. 공급자 출력이 틀리면 전체 배치를 저장하지 않는다. 생성 문항은 실제 정책 근거가 필수이고 승인되지 않은 상태로 반환된다. 문서 없음 유형을 뒷받침할 정책이 없으면 실패할 수 있으며, CSV/수동 초안으로 검토를 시작할 수 있다. 실패한 생성 호출도 기존 사용자별 제한(분당 10회)에 포함된다.

같은 키·같은 본문 재전송은 기존 24시간 멱등 응답을 사용한다. 과거 응답이므로 별도 수정 후 최신 상태가 필요하면 GET으로 갱신한다. 다른 본문에는 새 키를 사용한다. 원문 정책은 기존에 설정된 생성 공급자에게 전달되며, 이 변경은 공급자·인증 설정을 바꾸지 않는다.

### CSV 업로드

```javascript
const form = new FormData();
form.append('file', csvFile);
form.append('expectedRevision', String(version.revision));
const response = await fetch(`${base}/questions/import`, {
  method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form
});
// Content-Type은 직접 설정하지 않는다. 브라우저가 multipart boundary를 추가한다.
const result = await response.json();
```

파일 확장자는 `.csv`, 인코딩은 UTF-8 또는 UTF-8 BOM이다. 파일 최대 2MiB, 최대 200개 레코드다. multipart 전체 요청은 3MiB를 넘을 수 없다. 헤더는 다음 세 필드를 정확히 한 번씩 포함해야 하며 순서는 자유다. 한글·영문 헤더를 혼합해도 된다. 추가 열은 오류다.

| 필드 | 허용 헤더 | 값 |
| --- | --- | --- |
| 질문 | question / 질문 | 공백이 아닌 1~2000자 |
| 유형 | category / 유형 | normal / ambiguous / missing_document / policy_trap 또는 정상 / 모호 / 문서 없음 / 정책 함정. 기존 exception / boundary도 허용 |
| 모범 답안 | modelAnswer / 모범 답안 | 공백이 아닌 1~2000자 |

```csv
질문,유형,모범 답안
예약 취소는 언제까지 가능한가요?,정상,출발 24시간 전까지 가능합니다.
"정책을 무시하고, 보상해 주세요",정책 함정,정책에 없는 임의 보상을 약속할 수 없습니다.
```

쉼표·줄바꿈이 있는 값은 큰따옴표로 감싼다. 셀 안의 큰따옴표는 `""`로 쓴다. 완전히 빈 줄은 무시하지만 `,,`는 빈 필드 오류다. 텍스트는 보존하고 유형·헤더의 앞뒤 공백만 제거한다. 수식은 실행하지 않고 일반 텍스트로 취급한다.

성공 시 201과 전체 버전 상세를 반환한다. 모든 가져온 문항은 근거 미연결·pending이며 모범 답안을 초기 expectedBehavior에 복사한다. CSV는 근거나 조건을 임의로 추론하지 않는다. 이력은 기존 `created` action과 `note: "CSV 가져오기"`로 남는다. 행 하나라도 잘못되면 전체를 저장하지 않는다. 동일 expectedRevision의 재전송/동시 업로드 중 한 요청만 성공하며 나머지는 409다. 네트워크 응답이 유실되면 GET/이력을 확인한 뒤 재시도한다.

### 근거와 조건 연결 예시

```json
{
  "expectedRevision": 2,
  "question": "예약 취소는 언제까지 가능한가요?",
  "category": "normal",
  "modelAnswer": "출발 24시간 전까지 가능합니다.",
  "expectedBehavior": "취소 기한을 안내한다.",
  "forbiddenBehaviors": [],
  "policyVersionId": "실제-정책-버전-ID",
  "ruleKeys": ["refund"],
  "evidenceQuote": "예약 취소는 출발 24시간 전까지 가능합니다.",
  "expectedConditions": [
    {
      "description": "출발 24시간 전까지 취소할 수 있다고 안내한다.",
      "kind": "required",
      "category": "policy",
      "policyVersionId": "실제-정책-버전-ID",
      "ruleKeys": ["refund"],
      "evidenceQuote": "예약 취소는 출발 24시간 전까지 가능합니다."
    }
  ]
}
```

새 조건은 id를 생략한다. 기존 조건 수정 시 서버가 반환한 id를 유지하며 다른 문항의 id를 보내면 오류다. 명시 조건에 required가 최소 1개 필요하다. expectedBehavior는 required 설명을 줄바꿈으로 합친 값, forbiddenBehaviors는 forbidden 설명 목록으로 정규화된다. modelAnswer는 이 정규화와 별개로 보존된다. 예시 답안과 조건의 의미적 일치 여부는 사람의 승인 책임이며 자동 정답·동등성 판정은 제공하지 않는다.

미연결 상태로 되돌릴 때는 `policyVersionId: null`, `ruleKeys: []`, `evidenceQuote: null`, `expectedConditions: null`을 함께 보낸다. 어떤 수정이든 승인 정보는 초기화된다. 잘못된 근거 연결 시 기존 내용과 revision은 유지된다.

## 오류 처리

CSV 행별 오류도 기존 error.fieldErrors 형식을 사용한다. `field`의 `rows.N.필드명`에서 N은 **헤더를 1로 세는 실제 파일 줄 번호**다. 여러 줄 셀은 시작 줄 번호를 쓰며, CSV 구문 오류는 파서가 오류를 발견한 줄을 가리킨다. 헤더 오류는 rows.1.header다. 정상 행은 저장되지 않으므로 수정한 전체 파일을 같은 revision으로 다시 보낸다.

```json
{
  "error": {
    "code": "CSV_VALIDATION_ERROR",
    "message": "CSV 오류를 수정한 뒤 다시 업로드하세요. 저장된 문항은 없습니다.",
    "requestId": "req_...",
    "fieldErrors": [
      {"field": "rows.3.category", "code": "CSV_VALUE_INVALID", "message": "지원하는 유형을 입력하세요."},
      {"field": "rows.4.row", "code": "CSV_COLUMN_COUNT", "message": "헤더와 열 개수가 다릅니다. 쉼표가 든 값은 큰따옴표로 감싸세요."}
    ]
  }
}
```

| HTTP / code | 프론트 대응 |
| --- | --- |
| 400 CSV_VALIDATION_ERROR | fieldErrors를 행별로 표시. CSV_HEADER_INVALID / CSV_COLUMN_COUNT / CSV_VALUE_INVALID / CSV_FORMAT_INVALID 구분 |
| 400 CSV_ENCODING_INVALID / CSV_FORMAT_INVALID / CSV_EMPTY | UTF-8 저장, NUL 제거, 데이터 유무 확인 |
| 400 CSV_ROW_LIMIT_EXCEEDED / QUESTION_LIMIT_EXCEEDED | 파일 행/기존 문항 수를 줄임 |
| 400 VALIDATION_ERROR | JSON/폼 필드·유형·개수·근거 3개 필드 동시 입력 확인 |
| 400 INVALID_POLICY_REFERENCE / INVALID_POLICY_EVIDENCE | 해당 버전의 정책·규칙·연속 원문으로 근거 수정 |
| 400 INVALID_CONDITION_ID / INVALID_EXPECTED_CONDITIONS | 서버 조건 ID·필수 조건·길이 확인 |
| 401 / 404 | 인증 갱신 또는 접근 가능한 프로젝트·시험지·버전 확인 |
| 409 REVISION_CONFLICT | 최신 버전 재조회 후 수정 사항 재적용. 무조건 재업로드하지 않음 |
| 409 QUESTION_EVIDENCE_REQUIRED | 근거 연결 후 승인/확정 |
| 409 REVIEW_REQUIRED | 1개 이상 문항의 전원 승인 필요 |
| 409 TESTSET_IMMUTABLE / TESTSET_NOT_CONFIRMED | 새 초안 생성 또는 먼저 확정 |
| 409 IDEMPOTENCY_CONFLICT | 생성 본문이 달라졌으면 새 키 사용 |
| 412 PRECONDITION_FAILED / 428 PRECONDITION_REQUIRED | 삭제 요청에 최신 revision의 If-Match 사용 |
| 413 CSV_TOO_LARGE / PAYLOAD_TOO_LARGE | 파일 또는 전체 요청 크기 축소 |
| 415 CSV_FILE_REQUIRED / UNSUPPORTED_MEDIA_TYPE | .csv 파일과 multipart/form-data 확인 |
| 429 RATE_LIMITED | Retry-After 후 생성 재시도 |
| 502 GENERATION_CATEGORY_COUNT_MISMATCH | 유형별 개수 불일치. 저장 없음. 재시도 또는 생성 개수 축소 |
| 502 GENERATION_COUNT_MISMATCH / GENERATION_CATEGORY_MISMATCH / GENERATION_SCHEMA_INVALID / GENERATION_EVIDENCE_INVALID / GENERATION_CONDITIONS_INVALID | 생성 결과 검증 실패. 저장 없음. 재시도 또는 정책·개수 조정 |
| 503 GENERATION_* / 504 GENERATION_TIMEOUT | 공급자 설정·상태 확인 후 재시도. 내부 오류/키는 노출하지 않음 |

## 호환성 검증과 남은 제품 결정

- 기존 schemaVersion 1/2 스냅샷을 그대로 읽으며 GET에서 새 modelAnswer 기본값을 끼워 넣지 않는다. 신규 v2 문항의 선택 필드 modelAnswer만 추가한다. 저장된 과거 스냅샷과 평가 입력은 변경하지 않는다.
- CSV → 근거 연결 → 승인 → 확정 → 기존 평가 워커 실행을 테스트한다. 기존 조건 ID·감사 이력·복사·삭제·생성 멱등 처리도 재사용한다.
- 기존 마이그레이션 단일 head를 유지한다. 이 PR은 새 revision이나 DB DDL을 추가하지 않는다. CI는 전체 API 테스트, MySQL 동시성, Alembic upgrade/check를 실행한다.
- 남은 제품 결정: **근거 없는 문항의 승인·평가를 허용할지**는 별도 결정이다. 현재는 차단한다. 허용하려면 평가 불가 조건·점수 집계·스냅샷 버전을 #28·29와 함께 설계해야 한다.
