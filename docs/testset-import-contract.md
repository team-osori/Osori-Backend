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
