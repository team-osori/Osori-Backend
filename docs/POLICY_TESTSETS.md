# 정책·테스트셋 API 인계

## 이번 범위

텍스트 정책 원문과 규칙을 저장하고, 정책 버전을 근거로 질문·기대 행동·금지 행동을 작성하여 사람이 검토·확정하는 기능입니다. PDF/DOCX/TXT/MD 업로드와 한국어·영어 OCR 초안을 지원합니다.

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
- 502: GENERATION_SCHEMA_INVALID, GENERATION_COUNT_MISMATCH, GENERATION_CATEGORY_MISMATCH, GENERATION_EVIDENCE_INVALID, GENERATION_CONDITIONS_INVALID, GENERATION_DUPLICATE_QUESTIONS
- 503: GENERATION_NOT_CONFIGURED 또는 GENERATION_UNAVAILABLE
- 504: GENERATION_TIMEOUT

```bash
.venv/bin/python -m pytest -q
# 아래 DB는 반드시 전용 임시 테스트 DB여야 합니다.
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@127.0.0.1:3306/osori_test?charset=utf8mb4' .venv/bin/python -m pytest -q
.venv/bin/python -m alembic check
```

테스트의 가짜 생성기는 테스트에서만 의존성 주입으로 사용합니다. 서비스 기본값에는 연결되지 않습니다. 만료된 생성 재사용 레코드는 기존 `python -m app.maintenance` 명령으로 정리하며 자동 예약은 구성하지 않습니다.

## 정책 규칙 입력과 검증 (#21)

`POST /api/projects/{projectId}/policies`는 `name/content/rules`를 보내면 기존처럼 201과 저장된 정책을 반환합니다. `rules`를 생략하거나 null로 보내면 생성 모델이 규칙을 추출하고 **200과 미저장 초안**(`name/content/rules/status: draft/persisted: false`)을 반환합니다. 원문과 규칙을 검토·수정한 뒤 `name/content/rules`만 다시 보내 저장하세요. 정책 버전 생성에는 기존처럼 rules가 필수입니다. 질문 검토·확정은 별도의 draft 테스트셋에서 진행합니다.

`POST /api/projects/{projectId}/policies/import`는 multipart의 `name`, `file`, 선택 `ocrMode=auto|force`를 받습니다. PDF/DOCX/UTF-8 TXT/MD를 텍스트로 추출한 후 규칙 초안을 반환합니다. 업로드는 10MiB, PDF는 50페이지, 추출 원문은 100,000자까지입니다.

- `auto`(기본값): 일반 텍스트 추출을 먼저 수행하고, 텍스트가 없는 PDF 페이지만 Poppler로 렌더링해 Tesseract `kor+eng`으로 인식합니다. 텍스트 레이어를 읽지 못한 페이지도 OCR을 시도합니다. 텍스트와 이미지가 함께 있으면 텍스트를 사용하고 `imageTextPages`에 표시합니다. 스캔 본문이 누락되지 않았는지 검토하고 필요하면 `force`로 재처리하세요.
- `force`: 모든 PDF 페이지에 OCR을 적용합니다. 손상된 텍스트 레이어가 있는 문서를 다시 처리할 때 사용합니다. 다른 파일 형식에는 영향이 없습니다.
- OCR 결과는 오인식할 수 있으므로 원문과 규칙을 검토한 뒤 저장합니다. 표·다단·회전된 문서의 읽기 순서나 정확도를 보장하지 않습니다. OCR은 로컬에서 실행하지만 이후 규칙 추출은 기존 설정된 모델 제공자를 사용합니다.
- 초안의 `sourceDocument`는 `documentId`(파일 SHA-256 기반), `filename`, `contentHash`, `pageCount`(PDF 이외 null), `chunkCount`, `chunks`, `ocrPages`, `emptyPages`, `imageTextPages`를 포함합니다.
- 청크는 페이지를 넘지 않는 최대 2,000자 구간입니다. `index`/`pageNumber`는 1부터, `startOffset`/`endOffset`은 반환된 `content`의 Unicode 코드 포인트 기준 `[start,end)`입니다. JavaScript에서는 `[...content].slice(start,end).join('')`로 추출합니다. 문서 내 위치 키는 `(documentId, index)`입니다. 청크는 화면 근거 표시용이며 검색 인덱스가 아닙니다.
- OCR 후 빈 페이지는 `emptyPages`에 표시하며 페이지 번호를 당기지 않습니다. 읽을 수 있는 내용이 전혀 없으면 `DOCUMENT_TEXT_NOT_FOUND`(400), 암호화 PDF는 `DOCUMENT_ENCRYPTED`(400)입니다.
- 검토 후 `name/content/rules/sourceDocument`를 기존 정책 생성 API로 보내면 첫 버전에 저장합니다. 이후 버전 조회에도 위치 정보가 반환됩니다. 원문을 수정했다면 기존 위치는 유효하지 않으므로 `sourceDocument`를 제거/null로 보내세요. 해시·범위 불일치는 검증 오류로 거절됩니다. 위치 정보는 클라이언트가 반환한 검토용 메타데이터이며 원본 파일의 진위 증명이 아닙니다. 기존 정책 버전은 null입니다.

여러 파일은 `POST /api/projects/{projectId}/policies/imports`에 반복된 `files` 필드(1~5개)와 `ocrMode`를 보냅니다. HTTP 200의 `data.items`는 입력 순서를 유지하며 파일마다 `filename/status=draft/draft` 또는 `filename/status=failed/error`를 반환합니다. 오류에는 `code/message/httpStatus/retryAfter`가 포함됩니다. 일부 파일의 실패가 다른 파일의 초안을 취소하지 않습니다. 성공해도 저장되지 않으므로 각 초안을 개별 검토·저장하세요. 처리 중 상태는 프론트에서 관리하며, 즉시 개별 진행률이 필요하면 단일 `/import`를 파일별로 호출하세요. 배치는 순차 처리하므로 최대 5개 파일의 추출·모델 호출 시간을 고려해 클라이언트/프록시 제한 시간을 설정해야 합니다.

추출 요청은 사용자별 기존 rate limit(10회/윈도)에 포함되고 실패한 파일도 계산합니다. API 프로세스마다 최대 2개 문서를 동시 추출하며 초과하면 `DOCUMENT_EXTRACTION_BUSY`(503, 재시도 5초)를 반환합니다. 임시 PDF·이미지는 처리 후 제거하고 보관하지 않습니다. 추출 전체는 60초, 페이지 렌더링+OCR은 15초, 이미지 긴 변은 2,400px, 파일 출력은 16MiB로 제한합니다. Linux에서는 프로세스별 512MiB 메모리·45초 CPU를 제한하고, 전체 시간 초과 시 OCR 하위 프로세스까지 종료합니다.

`DOCUMENT_OCR_UNAVAILABLE`(503)는 실행 파일 또는 한국어/영어 모델 미설치, `DOCUMENT_OCR_TIMEOUT`/`DOCUMENT_TIMEOUT`(504)는 시간 초과, `DOCUMENT_OCR_FAILED`(400)는 인식 실행 실패입니다. 이 경우 일부 페이지만 추출된 초안을 성공으로 반환하지 않습니다.

Ubuntu 신규 호스트는 `deploy/bootstrap.sh`가 의존성을 설치합니다. **기존 호스트는 배포 전에** 아래 패키지를 설치해야 하며, 배포 preflight가 누락을 감지하면 현재 서비스를 중단하기 전에 실패합니다.

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng
tesseract --list-langs  # kor, eng 확인
```

macOS 개발 환경은 `brew install poppler tesseract tesseract-lang`을 사용합니다. 별도 모델 경로는 Tesseract의 `TESSDATA_PREFIX`로 지정할 수 있습니다. 운영 프로세스가 해당 경로를 읽을 수 있어야 합니다.

- [Tesseract 다국어 실행 문서](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
- [Poppler 렌더링 옵션](https://manpages.debian.org/bookworm/poppler-utils/pdftoppm.1.en.html)

모델은 현재 질문 생성의 제공사·모델·자격 증명 설정을 재사용합니다(Bedrock IAM 역할 또는 OpenAI 키). 규칙 추출은 별도 `PolicyExtractor` 경계이며 미설정/지원하지 않는 제공사는 `503 POLICY_EXTRACTION_NOT_CONFIGURED`로 명시합니다. Bedrock은 기존 Converse 호출 모듈을 재사용하며 배포·제공사 설정을 변경하지 않습니다. 모델 호출은 사용자별 분당 10회이며 실패도 포함합니다. `POLICY_EXTRACTION_INVALID/INCOMPLETE/REFUSED`(502), `POLICY_EXTRACTION_TIMEOUT`(504)를 구분합니다. 원문 인용은 공백 차이만 실제 원문으로 복원하며, 문서에 없는 근거나 중복 key는 저장 없이 거절합니다. 생성 결과의 의미적 적합성은 사용자가 검토해야 합니다.

- `content`: 공백뿐이 아닌 원문 1~100,000자. 원문의 공백·줄바꿈을 보존합니다.
- `rules`: 1~100개. `key`는 영문·숫자·밑줄·하이픈 1~100자이며 정책 버전 안에서 대소문자를 구분하여 유일해야 합니다.
- `description`: 공백뿐이 아닌 1~2,000자 설명.
- `sourceQuote`: 공백뿐이 아닌 1~10,000자이며 `content`에 **그대로 포함된 연속 부분 문자열**이어야 합니다. 공백·줄바꿈·문장부호를 정규화하거나 문장을 요약해서 보내면 일치하지 않습니다. 원문 문장을 그대로 복사하세요.

규칙 검증 실패는 다음처럼 문제 위치와 사유를 반환합니다. 원문 내용이나 제출 값은 오류에 포함하지 않습니다. 여러 오류가 있으면 먼저 발견된 규칙 오류부터 수정합니다.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "입력값을 확인해 주세요.",
    "requestId": "req_...",
    "fieldErrors": [{
      "field": "rules.1.key",
      "code": "POLICY_RULE_KEY_DUPLICATE",
      "message": "정책 버전 안에서 규칙 key는 중복될 수 없습니다."
    }]
  }
}
```

원문 불일치는 `field=rules.N.sourceQuote`, `code=POLICY_RULE_SOURCE_MISMATCH`입니다. 실패한 정책 생성·버전 생성은 내용·revision을 변경하지 않습니다. OpenAPI에도 동일한 입력 제약을 기재합니다. 문단을 자동으로 규칙으로 나누는 클라이언트는 저장 전에 사람이 규칙의 의미와 원문 근거를 검토해야 합니다.

## #27 시험지 가져오기 확장

CSV 가져오기, 신규 유형·유형별 생성 개수, 모범 답안, 미연결 근거 및 승인 규칙은 [시험지·CSV 연동 계약](testset-import-contract.md)을 참고하세요. 기존 API와 확정 스냅샷 v2는 유지됩니다.
