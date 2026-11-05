# #28·#29 평가 화면 및 #30 연동 계약

기존 단일 평가·조건별 검토·세 기준 판정과 실행 시 입력 스냅샷을 재사용한다.
기존 경로·필드는 유지하고 아래 필드를 추가한다. 시험지 모델 및 프롬프트 수정 제안
저장 영역은 변경하지 않는다. OpenAPI에 `ItemAnalysis`, `AnalysisSummary`,
`ScreenCounts`, `ScoreSummary`, `AnswerReviewCreate/Out`을 제공한다.

아래 경로의 공통 접두사는 `/api/projects/{projectId}/services/{serviceId}/evaluations`다.
모든 API는 기존 서비스 소유권 검증과 `data`/`error` 응답 형식을 사용한다.

## 실행과 화면 집계

1. `POST /`에 `connectionRevision`, `testSetVersionId`, 선택적 `promptVersionId`를 보내고
   `Idempotency-Key`를 사용한다. 단일 평가는 `mode: SINGLE` 또는 mode 생략으로 실행한다.
2. `GET /{evaluationId}`로 실행 상태를 폴링한다. QUEUED/RUNNING은 진행 중이며
   COMPLETED/PARTIAL_FAILED/FAILED/CANCELLED는 종료 상태다. 답변 처리 완료와 채점 완료는 다르다.
3. `GET /{evaluationId}/summary`의 `analysis.variants.a.screenCounts`로 화면 숫자를 표시한다.
   비교 실행은 `variants.b`도 있으며 단일 평가는 b를 생성하거나 실패로 세지 않는다.
4. `GET /{evaluationId}/items`의 `screenStatus`로 각 문항 상태를 표시한다.

| screenStatus | screenCounts | 집계 기준 |
| --- | --- | --- |
| CORRECT | correct | 성공한 답변의 모든 기준·조건이 판정 가능하고 모두 통과 |
| INCORRECT | incorrect | 모든 기준·조건이 판정 가능하고 하나 이상 미충족 |
| PENDING | pending | 호출·판정 처리가 진행 중(callStatus=RUNNING) |
| REMAINING | remaining | 아직 호출하지 않음; 취소 후 미호출 문항도 포함 |
| EXECUTION_ERROR | executionError | 호출 실패 또는 중단으로 결과 불확실 |
| JUDGMENT_ERROR | judgmentError | 답변은 있지만 판정 오류가 하나 이상 있음 |
| REVIEW_REQUIRED | reviewRequired | 미판정/조건 결과 누락/전체 판정 충돌/담당자 검토 보류 |

앞의 일곱 범주는 상호 배타적이고 합계는 `total`이다. 우선순위는 실행 상태 →
판정 오류 → 미판정 → 전체 O/X 충돌·보류 → 정답/오답이다. 남음이 0이어도 오류나
미판정이 있으면 전부 정답 또는 오답으로 채점된 것은 아니다. 종료한 취소 실행의
remaining도 자동으로 다시 처리되지 않는다. 기존 resume/rerun 계약을 사용한다.

`completedQuestions`는 처리 완료 수, `failedQuestions`는 실행 실패 수이며 각각 정답/오답
수가 아니다. `provisional`은 실행 진행 여부다. false여도 검토 필요 문항이 있을 수 있다.
`needsReviewCount`는 오답·오류·미판정·검토 충돌을 포함하므로 screenCounts의 reviewRequired와 다르다.

기존 `resultStatus`/`analysis.status`/`statuses`는 유지한다. 예를 들어 조건 미충족과
다른 기준의 판정 오류가 함께 있으면 기존 `resultStatus=UNMET`이지만 새
`screenStatus=JUDGMENT_ERROR`다. 화면 O/X 집계는 새 필드를 사용한다.

## 답변·조건·정책·해설 연결

`GET /{evaluationId}/items/{itemId}`에서 다음 자료를 함께 읽는다.

- `a.answer`: 기업 챗봇 원문 답변. 미호출/실패 시 null이며 가짜 오답을 만들지 않는다.
- `input`: 실행 시 고정한 질문·기대 조건. 시험지 원본 수정으로 바뀌지 않는다.
- `analysis.a.conditions[]`: conditionId, requirement, 필수/금지 구분, criterion,
  policyEvidence.policyVersionId/quote, judgment, automaticJudgment.
- `analysis.a.issues[]`: 미충족 조건, 실제 답변의 answerQuote 또는 missingGuidance,
  reason(해설), recommendation(안내). 사람이 실패로 검토한 결과에는 AI 권고를 새로 만들지 않는다.
- `a.evidenceCatalog`: 서버 발급 evidence_id와 종류·source_id의 대응.
  answer는 a.answer, policy는 `GET /{evaluationId}/inputs`의 testSet.policies,
  retrieval은 a.evidence.documents에 연결한다. 원문 전체를 읽을 때 현재 정책 대신 평가 스냅샷을 사용한다.

조건별 `status=judged`의 verdict는 pass/fail이며 not_evaluable/error는 verdict=null이다.
금지 조건의 pass는 금지 행동을 하지 않았다는 뜻이다. 문장 누락을 가짜 답변 인용으로 표시하지 않는다.

## 문항 전체 O/X 저장

사용자 확인: 전체 O/X는 **챗봇 답변 전체의 정답·오답**이다. 기존 조건별 판정을
덮어쓰지 않고 별도 담당자 검토 이력으로 저장한다.

`POST /{evaluationId}/items/{itemId}/answer-reviews`:

```json
{"expectedRevision":0,"side":"a","verdict":"pass","reason":"정책과 답변을 대조해 정답으로 검토했습니다."}
```

- pass=O, fail=X, not_evaluable=검토 보류. reason은 공백 아닌 필수 문자열이다.
- 종료한 평가에서 답변 호출이 성공한 side만 검토한다. 실행 중은 409 EVALUATION_NOT_TERMINAL,
  답변 없는 실패·미호출은 409 ANSWER_UNAVAILABLE, SINGLE의 side=b는 400 INVALID_REVIEW_TARGET이다.
- 조건 검토와 전체 판정은 **같은 문항 reviewRevision**을 공유한다. 어느 쪽을 수정해도 증가한다.
  오래된 revision은 409 REVISION_CONFLICT다. 최신 상세를 읽고 다시 검토한다.
- 상세 `analysis.a.answerReview`는 최신 전체 판정 또는 null, `reviewConflict`는 충돌 여부다.
- `derivedScreenStatus`는 전체 O/X 적용 전 기준·조건 집계다. 전체 O/X와 완전히 판정된 결과가
  반대면 screenStatus=REVIEW_REQUIRED로 표시한다. 조건이나 AI 원본을 일괄 변경하지 않는다.
- 담당자 O/X로 호출 실패·판정 오류·미판정을 정답/오답에 섞지 않는다. 필요한 조건별 검토를
  마친 뒤 정합성이 맞으면 자동으로 충돌이 해소된다. 기준별 AI 점수·통과율은 그대로다.
- `GET /{evaluationId}/items/{itemId}/answer-reviews`는 revision 역순의 전체 판정 이력이다.
  기존 `/reviews`와 `latestReviews`는 계속 **조건 검토만** 반환한다. 두 이력의 revision에는
  다른 종류의 검토 때문에 빈 번호가 생길 수 있다. 동일 타임스탬프여도 revision으로 정렬한다.
- 전체 O/X는 전후 비교 API의 조건별 해결/잔존/신규 판정을 변경하지 않는다.

## 세 평가 기준과 점수

신규 SINGLE과 명시적 COMPARE는 다음 세 기준을 쓴다.

| 키 | 표시 이름 | 점수/통과 |
| --- | --- | --- |
| policy | 정책 준수 | 점수 없이 pass/fail; scoreSummary=null |
| questionResolution | 질문 해결도 | 0~4점; 3점 이상 통과 |
| communicationQuality | 전달 품질 | 0~4점; 3점 이상 통과 |

`summary.criteria.{criterion}.{a|b}`의 passRate는 자동 판정
`PASS/(PASS+FAIL)*100`이고 0~100 백분율(소수 둘째 자리)이다. 분모가 0이면 null이다.
오류·미판정·미호출·중단은 분모에서 제외하고 기존 개별 카운터로 제공한다.

점수가 있는 두 기준의 `scoreSummary` 예:

```json
{"average":2.5,"denominator":2,"minimum":0,"maximum":4,"passThreshold":3,"basis":"AUTOMATIC"}
```

평균은 호출 성공·자동 판정 완료·실제 점수가 있는 문항만 집계한다. 0점은 유효한 점수이며
분모에 포함한다. 점수가 없으면 average=null, denominator=0이다. 평균 점수에 통과 임계값을
적용해 전체 통과율을 만들지 않는다. 사람 검토로 AI 점수를 생성하거나 원본 통과율을 덮어쓰지 않는다.

기존 v1의 retrieval/grounding/response/policy 네 기준과 기존 필드는 유지한다.
이를 세 기준으로 임의 변환하지 않는다. inputs.criteria.version/definitions로 화면을 분기한다.
v2의 retrieval/grounding은 조건의 진단 분류일 수 있지만 네 번째/다섯 번째 총점은 아니다.
과거 평가에 조건별 결과가 없으면 새 전체 O/X 화면은 미판정을 정답으로 추정하지 않는다.

## #30에서 사용할 오답 분석 계약

기존 `facts`, `hypotheses`, `causeStatus`, `confirmedCause`, `limitations`를 보존한다.
다음 확장 필드를 사용하면 기존 자동 분석과 현재 검토 결과를 구분할 수 있다.

- `hypotheses`: 자동 판정 당시 가설. 이후 담당자 검토로 정답이 되어도 원래 자료를 보존한다.
- `activeHypotheses`: 현재 screenStatus=INCORRECT이고 최신 조건 검토에서도 fail인 조건의 가설만 포함한다.
  **#30 수정 제안은 이 목록과 현재 issues를 사용**한다. 오류·검토 충돌 문항을 수정 가능 오답으로 세지 않는다.
- 가설은 conditionId, area(RETRIEVAL/PROMPT/MODEL), reason, evidenceRefs를 갖는다.
  근거 ID는 a.evidenceCatalog에 연결한다. `hypothesesBasis=AUTOMATIC`,
  `activeHypothesesBasis=LATEST_HUMAN_CONDITION_REVIEW_OVER_AUTOMATIC`이다.
- `promptContext`: source, available, promptVersionId, promptId, version, content,
  pointer, connectionRevision, applicationVerified.
  SELECTED_VERSION은 실행 시 보존한 버전 원문이다. CONFIGURED_TEMPLATE은 useConfiguredPrompt=true이고
  PROMPT_BODY 설정의 promptPointer가 문자열을 가리킬 때 그 위치의 내용만 제공한다.
  CONNECTION_ONLY의 기업 내부 프롬프트나 임의 requestTemplate 필드에서 내용을 추정하지 않는다.
  알 수 없으면 source=UNAVAILABLE, available=false, content=null이다. 빈 문자열은 알려진 빈 설정으로 보존한다.
- 설정 템플릿 원문을 볼 수 있어도 저장된 promptVersionId는 없을 수 있다. #30에서 버전 기반
  수정 제안·적용을 하려면 이를 구분해야 한다. applicationVerified=false인 원문을 기업 모델이
  실제 적용한 사실로 표시하지 않는다. 기존 facts/limitations는 자동 분석 당시의 의미를 유지한다.
- `facts`는 수집된 문서 수, 원문 문자열 포함 여부, 선택 버전 등 관측 사실이다.
  문자열 미포함만으로 의미상 검색 실패를 확정하지 않는다. documents=null은 미수집,
  documents=[]는 검색 자료 0개가 관측된 상태다.
- `confirmedCause`는 항상 null이다. MODEL 가설도 모델 내부 원인이 검증되었다는 의미가 아니다.
  통제된 인과 분리 실험을 수행하지 않았다는 limitations를 함께 표시한다.

| analysisStatus | 의미 |
| --- | --- |
| HYPOTHESIS_AVAILABLE | 현재 오답에 근거로 지지되는 활성 가설 있음 |
| UNDETERMINED | 오답이지만 지지 가능한 가설 없음 |
| UNAVAILABLE | 호출/판정 미완료, 오류, 미판정, 전체 O/X 충돌 또는 검토 보류 |
| NOT_APPLICABLE | 현재 정답이라 오답 분석 대상 아님 |

기존 causeStatus는 자동 가설 유무(HYPOTHESIS/UNDETERMINED)만 표현하므로 화면의
현재 분석 가능 상태에는 새 analysisStatus를 사용한다. 가설이 없음을 '원인 없음'으로 표시하지 않는다.

### 목록 필터와 문항 수

`GET /{evaluationId}/items?side=a&causeArea=PROMPT&screenStatus=INCORRECT`

- 신규 필터: side(a/b), screenStatus, causeArea, analysisStatus. 기존 resultStatus/issueKind/change와 AND로 결합한다.
- 기본 side=a. side=b는 COMPARE에서만 가능하고 SINGLE에서는 400 INVALID_ANALYSIS_SIDE다.
  목록의 resultStatus/screenStatus/analysisStatus/activeHypothesisAreas는 선택한 side 기준이다.
  문항 상세의 최상위 요약은 기존처럼 a 기준이며 `analysis.b`로 B 상세를 읽는다.
- 필터를 적용한 후 limit/cursor를 적용한다. 다른 필터·side의 커서는 400으로 거절한다.
  검토/진행 상태가 바뀐 경우 목록 첫 페이지와 summary를 다시 조회한다.
- `summary.analysis.variants.{side}.causeQuestionCounts`는 RETRIEVAL/PROMPT/MODEL별 문항 수다.
  동일 문항에 같은 영역의 실패 조건이 여러 개 있어도 한 번만 센다.
- 한 문항은 여러 영역에 속할 수 있어 합계가 오답 문항 수보다 클 수 있다.
  causeCountUnit=QUESTION, causeCountsIncludeMultipleAreasPerQuestion=true다.
- `analysisStatusCounts`는 위 네 분석 상태별 문항 수이며 상호 배타적이다.
  모든 요약은 페이지와 무관하게 실행 전체를 집계한다.
- 기존 issueTypes는 조건 문제 건수이므로 문항 수와 다를 수 있다.

## 저장과 검증

마이그레이션 `20261105_2829`는 `evaluation_answer_reviews`만 추가한다.
기존 평가·조건 검토·시험지·프롬프트 데이터를 수정하지 않으며 revision 충돌은
기존 evaluation_items.review_revision의 원자적 비교/증가로 막는다.
현재 down_revision은 `20261105_11`이며 PR 제출 직전 최신 main과 연결을 다시 확인한다.
다른 작업에서 마이그레이션이 먼저 병합되면 미배포 상태에서 연결 관계를 조정해야 한다.

회귀 검증: 상태 혼합·분모/0점·조건 누락·취소/재개·A/B 분리·원인 영역 중복·필터/페이지·
프롬프트 스냅샷·담당자 검토 후 활성 가설·O/X 충돌·권한·이력·동시 revision.
MySQL의 전체 판정/조건 검토 경합 및 Alembic 모델 일치는 CI에서 확인한다.
테스트의 기업 API와 판정 모델은 대역이며 실제 기업 API/Bedrock 호출은 하지 않는다.
Figma 연결 노드 306:3378은 로그인 화면이므로 최종 평가 화면과의 시각적 대조 및 실제
프론트 O/X 저장 동작은 연동 검증이 남는다.
