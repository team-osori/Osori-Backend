# 프롬프트 수정 제안 입력 계약 (#30·31)

기준: origin/main `a68eba7`. 별도 `prompt-suggestions` 모듈이 기존
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
