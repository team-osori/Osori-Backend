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
