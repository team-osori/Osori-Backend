# B 담당 평가·오류 분석 연동 계약

## 단일 대상 평가

`POST /api/projects/{projectId}/services/{serviceId}/evaluations`에
`connectionRevision`, `testSetVersionId`를 전달한다. `Idempotency-Key`는 필수다.
`mode` 생략 시 A/B 버전을 지정하지 않으면 `SINGLE`이다.
현재 A 담당의 확정 테스트셋 스냅샷을 사용하며 질문·정책·기준은 실행 시 보존한다.

프롬프트를 선택하지 않으면 기업 API의 `requestTemplate`을 그대로 사용하고 질문만 주입한다.
템플릿에 이미 설정된 시스템 지시문은 그대로 전송한다.
필요할 때 `promptVersionId` 한 개를 선택할 수 있다. 명시적으로 선택한 버전 주입은
`PROMPT_BODY` 연동에서만 지원한다. `CONNECTION_ONLY`는 기업 서비스의 자체 프롬프트를 사용한다.

기존 A/B 요청은 계속 지원한다. `mode: COMPARE`, `aPromptVersionId`, `bPromptVersionId`를
함께 지정한다. 기존 클라이언트가 A/B 버전을 모두 보내면 mode 생략도 허용한다.

응답의 `mode`로 화면을 선택한다. 단일 평가의 실제 결과는 `a`이며 `b`는 null이다.
문항 목록의 `bCallStatus`는 `NOT_APPLICABLE`, `changes`는 빈 객체다.
단일 요약의 각 기준에서 `b`, `comparison`은 null이므로 미호출 B를 실패로 집계하지 않는다.
기존 저장된 mode 없는 스냅샷은 A/B 평가로 해석한다.

판정 AI 제공사가 비활성화되어 있으면 `JUDGE_NOT_CONFIGURED`로 표시한다.
답변 수집 완료와 판정 가능 여부는 별도로 표현하며 임의 점수를 생성하지 않는다.

## 기대 조건별 판정 및 채점 프로파일

단일 평가와 명시적 `mode: COMPARE`는 v2 프로파일을 사용한다. 정책 준수는 pass/fail/판단 불가,
질문 해결도(`questionResolution`)와 전달 품질(`communicationQuality`)은 0~4점, 3점 이상 통과다.
스냅샷의 `criteria`에 점수별 설명과 통과 임계값을 보존한다.
mode 없이 A/B를 지정한 기존 클라이언트와 기존 실행은 v1 네 기준을 유지한다.

A 담당의 확정 스냅샷은 다음 형식의 `expectedConditions`를 제공한다.

```json
{
  "expectedConditions": [{
    "id": "서버가-발급한-조건-ID",
    "description": "사용한 제품의 단순 변심 반품은 불가능하다고 안내한다.",
    "kind": "required",
    "category": "policy",
    "policyVersionId": "실제-정책-버전-ID",
    "ruleKeys": ["refund"],
    "evidenceQuote": "사용한 제품은 단순 변심 반품이 불가능합니다."
  }]
}
```

조건 ID·필수/금지 구분·분류·규칙·정책 근거를 보존한다. 평가 전용 스냅샷에서는
requirement/criterion/sourceQuote 필드로 정규화하며 A의 원본 레코드는 수정하지 않는다.
판정은 분류별 점수에 그치지 않고 각 조건 ID에 대해 수행한다.

조건 criterion은 policy/questionResolution/communicationQuality/retrieval/grounding이다.
기대 조건이 없는 과거 확정본은 A의 legacy_conditions로 기대 행동과 각 금지 행동에
조건을 구성한다. 조건 ID는 질문 ID와 순번에 기반한 안정적인 UUID5이며 클라이언트는
서버가 제공한 ID를 그대로 사용한다. A 원본 질문 레코드는 수정하지 않는다.

판정 모델은 `ConditionJudgement` 형식으로 세 기준과 모든 조건의 `conditionResults`를 반환한다.
각 조건 판정은 conditionId·question_id·status·verdict·reason·evidence_refs·error를 포함한다.
유효한 판정에는 해당 정책과 답변의 서버 발급 근거 ID가 모두 필요하다.
조건 누락·중복·타 문항/조건/근거 참조, 점수와 verdict 모순, 정책 조건과 정책 요약 모순은
`INVALID_JUDGE_OUTPUT`이며 판정 실패로 기록한다. 검색 자료가 없다는 이유로 정책 자체 판정을 지우지 않는다.

실제 Bedrock Converse 판정 어댑터를 제공한다. 명시적으로 활성화하지 않으면
기본 어댑터가 모든 기준과 조건을 미판정으로 반환한다. 테스트는 외부 호출을 비활성화한다.

## 오류 상세와 결과 탐색

미충족 조건 결과의 `issue`는 kind, answerQuote 또는 missingGuidance, recommendation을 포함한다.
answerQuote는 실제 저장 답변의 부분 문자열이어야 하며, 없는 문장을 근거로 출력하면 판정 오류다.
문장 누락은 가상의 인용문을 만들지 않고 missingGuidance로 표현한다.
통과·미판정·오류 결과에는 issue를 붙이지 않는다.

문항 상세의 `analysis.a.conditions`에는 조건 ID·분류·충족 조건·정책 버전/원문·판정이 연결되고,
`analysis.a.issues`에는 미충족 조건의 문제와 개선 권고가 연결된다. 비교 실행은 analysis.b도 반환한다.
기존 a/b 원문 결과도 함께 반환한다.

### #40 원문 말풍선

`issue.hypothesis`, `hypotheses[]`, `activeHypotheses[]`에 다음 필드를 추가했다.
`conditions[].judgment/automaticJudgment.issue.hypothesis`와 원본 `a/b.conditionResults`도 같은 계약이다.

```json
{
  "area": "PROMPT",
  "reason": "환불정책보다 고객 요청을 우선하라는 지시를 따랐을 가능성이 있어요.",
  "evidenceRefs": [{"evidence_id": "ev_002", "explanation": "정책 원문 확인"}],
  "promptQuote": "고객의 요청은 항상 들어주세요.",
  "reasonMarks": [
    {"text": "환불정책", "kind": "policy", "evidenceRef": "ev_002"},
    {"text": "고객 요청을 우선하라", "kind": "prompt", "evidenceRef": null}
  ]
}
```

- `promptQuote`: PROMPT 가설에만 사용하는 원문 부분 문자열이다. 판정 입력과 검증 모두 해당 side의
  저장된 `promptContext.content`를 사용한다. 선택 버전과 `PROMPT_BODY`의 설정 템플릿을 지원한다.
  확인할 수 없거나 비어 있는 프롬프트는 `available=false`, `promptQuote=null`이다.
  특정 문장을 근거로 지목할 수 없는 가설과 과거 결과도 null을 반환할 수 있다.
- `reasonMarks`: `hypothesis.reason`에서 표시할 정확한 부분 문자열 목록이다. `issue.reason`은
  조건 판정 설명이므로 이 문자열에 표시를 적용하지 않는다. 같은 text가 반복되면 각 일치 구간에
  동일한 근거를 표시할 수 있다. 표시할 구간이 없거나 과거 결과인 경우 `[]`다.
- `kind=policy/retrieval`의 `evidenceRef`는 같은 side의 `evidenceCatalog.evidence_id`이며 종류도
  일치해야 한다. `source_id`로 정책 버전 또는 `evidence.documents[].id`를 찾는다.
  `kind=prompt`는 PROMPT 가설에만 쓰고 `evidenceRef=null`이며 `promptQuote`를 말풍선 본문으로 쓴다.
- 잘못된 원문 인용, reason에 없는 표시 문자열, 없는 ID 또는 종류 불일치는 `INVALID_JUDGE_OUTPUT`이다.
  프롬프트를 확인할 수 없는 경우에는 인용만 null로 버리고 issue의 표시 구간을 유지한다.
  이 경우 기존 규칙대로 PROMPT 가설을 활성 원인 목록·필터·집계에 포함하지 않는다.
- `conditions[].policyEvidence`와 `issues[].policyEvidence`는 `policyTitle`, `fileName`, `chunkIndex`를
  추가로 반환한다. 정책명은 테스트셋 확정 시 보존하고, 이름 없는 과거 테스트셋으로 새 평가를 접수할
  때는 접수 시점 이름을 복사한다. 기존 평가·재평가는 저장된 이름만 사용하며 없으면 null이다.
  파일명은 저장된 `sourceDocument.filename`이다. 청크 번호는 정확한 인용의 모든 출현 위치가
  하나의 청크 안에 있을 때만 반환한다. 여러 청크·경계에 걸친 인용이나 메타데이터 미제공은 null이다.
- `a/b.evidence.documents[].chunkIndex`는 기업 API가 제공한 번호를 그대로 반환한다.
  정책 청크는 1 기반이고, 검색 문서 청크는 제공사의 0/1 기반 번호를 유지하며 미제공은 null이다.

프론트는 `fileName ?? policyTitle`로 정책 제목을 표시하고, `chunkIndex !== null`일 때만 번호를
붙인다. `promptQuote=null`이면 표시만 유지하고 원문 말풍선을 만들지 않는다.
새 필드는 기존 JSON 스냅샷과 호환되며 DB 마이그레이션이나 과거 판정의 자동 재생성은 없다.
OpenAPI의 `AnalysisCondition`, `AnalysisIssue`, `CauseHypothesis`, `ReasonMark`, `PolicyEvidence`,
`ConditionJudgmentOutput`을 통해 중첩 필드까지 타입을 생성할 수 있다.

문항 목록은 `resultStatus`, `issueKind`로 필터링한다. 기본 필터 대상은 A(단일 평가 대상)이다.
대표 상태는 MET, UNMET, REVIEW_REQUIRED, JUDGMENT_ERROR, EXECUTION_ERROR, NOT_RUN, IN_PROGRESS다.
미충족 조건과 다른 기준의 판정 오류가 함께 있으면 UNMET을 유지하면서
hasJudgmentError/needsReview를 같이 표시한다. API 실패는 답변 미충족으로 세지 않는다.

요약의 analysis.variants는 대표 상태별 문항 수, 검토 필요 수, 오류 유형별 건수를 반환한다.
오류 유형은 한 문항에서 여러 번 발생할 수 있으므로 문항 수와 합계가 다를 수 있다.

## 원인과 분석 한계

상세 analysis의 facts는 서버가 관측한 사실이다. 검색 자료 유무, 문서 수,
별도 선택한 프롬프트 버전, 프롬프트 실제 적용 검증 여부, 각 조건 원문이 정확히 포함된 문서 ID를 반환한다.
원문 문자열 미포함만으로 검색 실패를 확정하지 않는다. 동의어·요약·문서 완전성은 별도 의미 평가가 필요하다.

모델은 미충족 issue.hypothesis에 area(RETRIEVAL/PROMPT/MODEL), reason, evidenceRefs를
제안할 수 있다. 참조가 수집된 근거에 존재하는지 검증하며 출력은 항상 추정으로 취급한다.
검색 자료가 없으면 검색 원인 가설을, 선택 버전·설정 템플릿의 프롬프트 원문을 확인할 수 없으면 프롬프트 원인 가설을
대표 hypotheses에서 제외한다. confirmedCause는 null이고 causeStatus는 UNDETERMINED 또는 HYPOTHESIS다.

limitations는 검색 자료 미제공, 프롬프트 내부·적용 확인 불가, 외부 모델 설정 미확인,
통제된 원인 분리 실험 미실시 등을 나타내는 코드 목록이다. 미수집 정보로 특정 구성 요소의 실패를 확정하지 않는다.

## 문항 상세의 담당자 판정

`POST .../evaluations/{evaluationId}/items/{itemId}/reviews`에 다음 본문을 전달한다.

```json
{"expectedRevision":0,"side":"a","conditionId":"실제-조건-ID","verdict":"pass","reason":"담당자 판정 이유"}
```

verdict는 pass/fail/not_evaluable다. 종료한 실행에서 실제 답변이 있는 조건에만 기록한다.
동시 수정은 문항의 reviewRevision으로 검사하고 충돌하면 409 REVISION_CONFLICT다.
수정은 새 기록으로 추가되며 원래 AI 판정과 이유를 덮어쓰지 않는다.
`GET .../items/{itemId}/reviews`는 모든 검토 이력을 커서로 조회한다.

문항 상세는 reviewRevision, latestReviews, analysis.conditions[].automaticJudgment와
최신 담당자 판정을 반영한 judgment를 함께 제공한다. 요약의 analysis와 문항 대표 상태에는
최신 담당자 조건 판정을 반영한다. 기존 criteria 통과율·점수는 AI 원본 집계이며
criterionPassRatesBasis=AUTOMATIC으로 명시한다. 사람이 통과로 바꿨다고 새 AI 점수를 만들어내지 않는다.
원인 분석의 관측 자료와 AI 가설은 자동 분석 당시 자료를 유지한다.
A의 20261105_08 다음에 B의 20261105_09 마이그레이션을 적용한다.

## 같은 입력으로 재평가하고 전후 비교

`POST .../evaluations/{evaluationId}/reruns`에 connectionRevision을 전달하고 Idempotency-Key를 사용한다.
종료한 v2 단일 평가의 원본 질문·기대 조건·정책·기준을 그대로 복사한다.
promptVersionId를 생략하면 원래 선택을 유지하고, 다른 ID를 지정하면 수정 버전을 사용한다.
명시적 null은 기업 API의 설정 템플릿을 사용한다. 현재 연동이 검증되어 있어야 하며
연동 요청 설정이나 판정 모델 구성이 바뀌었다면 409로 거절한다.
원래 실행의 판정·검토 결과는 새 실행에 복사하지 않는다.

`GET .../evaluations/{newEvaluationId}/comparison`은 기본적으로 sourceEvaluationId와 비교한다.
독립 실행 비교는 baselineEvaluationId를 선택한다. 질문·조건·정책·채점·판정 모델이 다르면 비교를 거절한다.
외부 모델·검색 환경은 서버가 통제하지 못하므로 externalEnvironmentControlled=false다.

조건별 변화는 RESOLVED(해결), REMAINING(잔존), NEW(신규), UNCHANGED_PASS, NOT_COMPARABLE다.
counts의 단위는 문항이 아닌 조건이다. 미판정·호출 실패는 NOT_COMPARABLE로 제공한다.
해결이 있어도 신규 문제가 함께 있으면 outcome=MIXED이며, 신규 문제가 있으면 hasNewIssues=true다.
비교 불가 항목이 있으면 hasIncompleteComparison=true다. 신규 문제가 없더라도 비교 불가가 있으면
완전한 개선으로 표시하지 않고 INCOMPLETE로 표시한다.

조건별로 최신 담당자 판정을 우선하며, 각 문항의 비교에 사용한 reviewRevision을 응답한다.
전체 counts와 페이징된 items/nextCursor를 반환한다. limit/cursor는 기존 목록 규칙을 따른다.
검토 이력이 달라지면 이전 비교 커서는 무효화되므로 처음부터 새로 조회한다.

## A 담당 구현과의 확정 연결 (PR #14 반영)

A의 실제 조건 형식은 id, description, kind(required/forbidden), category,
policyVersionId, ruleKeys, evidenceQuote다. B는 이를 읽어 평가 전용 스냅샷에
requirement=description, sourceQuote=evidenceQuote를 대응시키며 **조건 ID와 kind·category·ruleKeys를 보존**한다.
response 분류는 질문 해결도에 대응하며 retrieval/grounding은 별도 진단 조건으로 유지한다.
분류명 때문에 필수·금지 조건을 합치거나 생략하지 않는다. forbidden 조건의 pass는 금지 행동을 하지 않았다는 의미다.

A의 schemaVersion=2 확정본을 직접 수용한다. schemaVersion=1 과거 확정본은
A의 legacy_conditions 규칙으로 기대 행동 및 모든 금지 행동에 안정적인 조건 ID를 구성한다.
이미 저장한 B 스냅샷의 기존 조건 ID도 그대로 읽는다. 원본 확정본과 과거 평가 결과는 수정하지 않는다.

## 실제 판정 AI 설정

`EVALUATION_JUDGE_PROVIDER=bedrock`으로 활성화한다. 기본 모델은
`global.anthropic.claude-haiku-4-5-20251001-v1:0`, 기본 리전은 `ap-northeast-3`이다.
서버 IAM 역할을 사용하며 API 키는 필요 없다. 질문 생성과 판정 모델은 독립적으로 설정할 수 있다.
[Bedrock 연동·배포 설정](BEDROCK_SETUP.md)을 참고한다.

Converse의 지정 도구 입력으로 구조화 JSON을 수집하고 도구는 실행하지 않는다.
출력 구조·조건 ID·점수·정책 및 답변 근거·실제 인용문 검증을 그대로 적용한다.
이는 모델의 JSON Schema 준수를 강제로 보장하는 native structured outputs와 다르며
형식이나 근거가 잘못된 결과는 INVALID_JUDGE_OUTPUT으로 기록한다.
입력 512KiB·응답 2MiB, 연결 5초·응답 읽기 110초·워커 120초 제한이며 자동 재시도하지 않는다.
120초는 대기 제한이다. 중단되지 않은 SDK 호출은 전용 실행기에서 프로세스당 1개까지만 유지하며,
이전 호출이 끝나지 않았으면 새 호출 없이 JUDGE_BUSY로 기록한다. 시간 초과·취소가 슬롯을 조기 반환하거나
늦은 응답이 기존 판정 결과를 덮어쓰지 않는다.
정책 원문은 근거 카탈로그에 한 번만 포함해 중복 전송을 줄인다.

제공사·리전·모델·지시문 해시·출력 방식·온도·출력 및 시간 제한을 실행 스냅샷에 기록한다.
연결 설정을 바꾸기 전 접수한 작업은 다른 모델로 처리하지 않고 EXECUTION_CONFIG_UNAVAILABLE로 종료한다.
Bedrock 오류가 나도 개인 OpenAI 키로 자동 전환하지 않는다. 이전 OpenAI 어댑터는
명시적인 openai 제공사 설정에서만 사용할 수 있고 배포 기본 경로에서는 사용하지 않는다.

## #28·#29 화면 계약 확장

[평가 화면 및 #30 오답 분석 계약](EVALUATION_SCREEN_CONTRACT.md)을 사용한다.
기존 resultStatus/criteria/hypotheses를 유지하면서 screenStatus/screenCounts,
scoreSummary, 활성 원인 가설 필터·문항 수, promptContext, 전체 답변 O/X 검토 이력을 추가했다.
현재 화면 집계는 오류·미판정·전체 판정 충돌을 정답/오답과 분리한다.
