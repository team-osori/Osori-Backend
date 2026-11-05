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

아직 제공사를 정하지 않은 판정 AI는 `JUDGE_NOT_CONFIGURED`로 표시한다.
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

모델 제공사와 실제 API 형식은 미정이다. get_evaluation_judge의 어댑터 연결부를 사용하며
현재 기본 어댑터는 모든 기준과 조건을 미판정으로 반환한다. 테스트 어댑터는 테스트에서만 사용한다.

## 오류 상세와 결과 탐색

미충족 조건 결과의 `issue`는 kind, answerQuote 또는 missingGuidance, recommendation을 포함한다.
answerQuote는 실제 저장 답변의 부분 문자열이어야 하며, 없는 문장을 근거로 출력하면 판정 오류다.
문장 누락은 가상의 인용문을 만들지 않고 missingGuidance로 표현한다.
통과·미판정·오류 결과에는 issue를 붙이지 않는다.

문항 상세의 `analysis.a.conditions`에는 조건 ID·분류·충족 조건·정책 버전/원문·판정이 연결되고,
`analysis.a.issues`에는 미충족 조건의 문제와 개선 권고가 연결된다. 비교 실행은 analysis.b도 반환한다.
기존 a/b 원문 결과도 함께 반환한다.

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
검색 자료가 없으면 검색 원인 가설을, 선택 프롬프트가 없으면 프롬프트 원인 가설을
대표 hypotheses에서 제외한다. confirmedCause는 null이고 causeStatus는 UNDETERMINED 또는 HYPOTHESIS다.

limitations는 검색 자료 미제공, 프롬프트 내부·적용 확인 불가, 외부 모델 설정 미확인,
통제된 원인 분리 실험 미실시 등을 나타내는 코드 목록이다. 미수집 정보로 특정 구성 요소의 실패를 확정하지 않는다.
## A 담당 구현과의 확정 연결 (PR #14 반영)

A의 실제 조건 형식은 id, description, kind(required/forbidden), category,
policyVersionId, ruleKeys, evidenceQuote다. B는 이를 읽어 평가 전용 스냅샷에
requirement=description, sourceQuote=evidenceQuote를 대응시키며 **조건 ID와 kind·category·ruleKeys를 보존**한다.
response 분류는 질문 해결도에 대응하며 retrieval/grounding은 별도 진단 조건으로 유지한다.
분류명 때문에 필수·금지 조건을 합치거나 생략하지 않는다. forbidden 조건의 pass는 금지 행동을 하지 않았다는 의미다.

A의 schemaVersion=2 확정본을 직접 수용한다. schemaVersion=1 과거 확정본은
A의 legacy_conditions 규칙으로 기대 행동 및 모든 금지 행동에 안정적인 조건 ID를 구성한다.
이미 저장한 B 스냅샷의 기존 조건 ID도 그대로 읽는다. 원본 확정본과 과거 평가 결과는 수정하지 않는다.
