# #28·#29 평가 화면 및 #30 연동 계약

기존 단일 평가·조건별 검토·세 기준 판정과 immutable inputs를 재사용한다.
기존 API 경로와 응답 필드는 유지하고 아래 필드를 추가한다. 시험지 모델과 프롬프트
수정 제안 저장 영역은 변경하지 않는다.

## #30에서 사용할 오답 분석

- 상세: `GET .../evaluations/{evaluationId}/items/{itemId}`의 `analysis.a`.
- 원본 답변: `a.answer`; 조건/정책 근거/해설: `analysis.a.conditions`, `issues`.
- 기존 `facts`, `hypotheses`, `causeStatus`, `confirmedCause`, `limitations`를 보존한다.
- `hypotheses`는 자동 판정 당시 가설이다. `activeHypotheses`는 최신 조건별 검토에서도
  실패로 남은 조건의 가설만 포함한다. 수정 제안은 이 목록과 현재 `issues`를 사용한다.
- 가설은 `conditionId`, `area`(RETRIEVAL/PROMPT/MODEL), `reason`, `evidenceRefs`를 갖는다.
  근거 ID는 해당 side의 `evidenceCatalog`에 연결한다. 어떤 가설도 확정 원인이 아니다.
- `promptContext`는 실행 시 보존한 선택 프롬프트의 ID·버전·원문을 제공한다.
  별도 버전이 없는 기업 자체 프롬프트는 내부를 추측하지 않고 unavailable로 표시한다.
- `analysisStatus`: HYPOTHESIS_AVAILABLE(활성 가설 있음), UNDETERMINED(오답이지만
  지지 가능한 가설 없음), UNAVAILABLE(호출/판정/검토 미완료), NOT_APPLICABLE(정답).
- 목록 필터 `causeArea`, `analysisStatus`, `side`를 추가한다. 기존 필터와 AND로 결합한다.
  기본 side=a이며 단일 평가에 side=b는 오류다.
- 요약 `analysis.variants.{side}.causeQuestionCounts`는 원인 영역마다 문항 ID를 한 번만 센다.
  한 문항이 여러 영역에 속할 수 있으므로 영역 합계는 전체 오답 문항 수와 다를 수 있다.

## 화면 집계

`analysis.variants.{side}.screenCounts`는 correct/incorrect/pending/remaining/
executionError/judgmentError/reviewRequired/total을 제공한다. 앞의 일곱 범주는 상호 배타적이다.
`pending`은 호출 진행 중, `remaining`은 미호출이며 취소된 미호출도 포함한다.
`correct`와 `incorrect`는 전체 판정이 가능한 문항만 센다. 호출 실패·중단,
판정 오류, 미판정은 각각 별도 범주다. 혼합 오류가 있는 기존 resultStatus=UNMET은
호환성을 유지하되 화면은 새 `screenStatus`를 사용한다.
실행 `completedQuestions`는 처리 완료 수이며 정답 수가 아니다.

## 세 기준

신규 단일 평가와 명시적 COMPARE는 policy/questionResolution/communicationQuality다.
정책 준수는 점수 없이 pass/fail이며 나머지 둘은 0~4점, 3점 이상 통과다.
기존 v1의 retrieval/grounding/response/policy 결과를 세 기준으로 임의 변환하지 않는다.
기준별 통과율은 자동 판정 PASS/(PASS+FAIL)*100, 판정 분모가 0이면 null이다.
`scoreSummary`는 점수가 있는 자동 판정만의 평균/분모/범위를 제공한다.
담당자 검토로 AI 점수를 만들거나 원본 기준 통과율을 덮어쓰지 않는다.

## 문항 전체 O/X

이슈 본문만으로 저장 의미가 확정되지 않았다. 연결된 Figma node 306:3378은 로그인
화면이므로 사용자에게 별도 판정/조건 일괄 판정/자동 집계 중 의미를 질문했다.
확정 전에는 기존 조건별 검토 저장과 그 결과의 전체 집계를 유지한다.
