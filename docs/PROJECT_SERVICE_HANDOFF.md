# 프로젝트 목록·생성·서비스 연결 — 이슈 #25

[이슈 #25](https://github.com/team-osori/Osori-Backend/issues/25)의 백엔드 작업입니다. 피그마의 `305:3366` 프로젝트 목록과 `305:3368` 생성 모달을 확인했습니다. 이슈 링크의 `306:3378`은 로그인 폼이어서 관련 화면을 찾아 확인했습니다. 이메일 인증 전환(#24)은 별도 담당 작업입니다.

## 목록·상세

`GET /api/projects?search=모아몰&status=SETUP&limit=20`에서 이름 부분 검색과 상태 필터를 지원합니다. `status`는 생략(전체), `SETUP`(준비 중), `RUNNING`(진행 중), `DONE`(완료)입니다. 검색어는 공백 제거 후 1~100자이며 `%`, `_`도 일반 문자로 검색합니다.

검색·상태 조건은 페이지를 자르기 전에 적용합니다. 다음 페이지는 동일한 검색·상태와 `nextCursor`로 요청합니다. 조건을 바꾸면 커서를 버리세요. 다른 조건·사용자의 커서는 `400 INVALID_CURSOR`입니다.

목록 `data`에는 기존 `items`, `nextCursor`에 아래 필드가 추가됩니다.

- `total`: 검색에 맞는 접근 가능 프로젝트의 전체 개수. 상태 필터·페이지 제한 적용 전입니다.
- `statusCounts`: 같은 검색 범위의 `SETUP`, `RUNNING`, `DONE`별 개수. 사이드바에 사용합니다.

소유 프로젝트와 활성 멤버인 프로젝트만 반환하며 휴지통 프로젝트는 제외합니다. 삭제된 테스트셋의 확정 기록은 진행 중으로 집계하지 않습니다. 상태는 기존 `progress.status`와 같은 의미입니다. 평가 실패·중단도 평가 이력이므로 `RUNNING`에 해당하며, 실제 실행 상태는 `latestEvaluation.status`로 구분합니다.

각 프로젝트 목록·상세 응답에는 다음이 추가됩니다.

- `service`: 없으면 null. 있으면 `id`, `name`, `revision`, `hasConnection`, `endpointHost`, `connectionRevision`, `connectionVerified`입니다. 전체 URL·쿼리·인증키는 포함하지 않습니다.
- `latestEvaluation`: 없으면 null. 최신 접수 평가의 `id`, `status`, `mode`, `criteriaVersion`, `createdAt`, `finishedAt`, `provisional`, `criteria`입니다. 진행 중에는 `provisional=true`입니다.

`criteria`는 해당 평가 `/summary`의 기준별 집계와 동일합니다. `passRate = pass / (pass + fail) × 100`이며 판정한 항목이 없으면 null입니다. 호출 실패·판정 실패·판단 불가는 별도 건수입니다. SINGLE은 a만 있고 b/comparison은 null입니다. COMPARE는 양쪽과 비교 집계를 제공합니다. 사람 재판정과 혼합하지 않은 AI 원본 집계입니다.

피그마의 검색·근거·응답·정책 네 열은 예전 기준(v1)입니다. 현재 조건별 평가(v2)는 정책·질문 해결·소통 품질 세 기준이므로 `criteriaVersion` 및 실제 `criteria` 키에 따라 렌더링하세요. 존재하지 않는 기준을 점수 0으로 채우지 않습니다. 최신 접수 평가 정보와 `progress.lastEvaluatedAt`(마지막 종료 시각)은 서로 다른 값입니다.

## 생성과 실패 복구

기존 `POST /api/projects`에 선택 `service`가 추가됩니다. 프로젝트·서비스를 하나의 트랜잭션으로 저장하고 둘 중 하나라도 실패하면 모두 취소합니다. 연결 설정·비밀값은 기존 연결 API에서 저장하며 프로젝트 생성 요청에 넣지 않습니다.

```http
POST /api/projects
Authorization: Bearer <access-token>
Idempotency-Key: <생성 시도마다 만든 UUID>
Content-Type: application/json

{"name":"모아몰 CS 챗봇","description":"고객 상담 평가","service":{"name":"CS 챗봇"}}
```

성공은 201과 `Location`이며 `data.service.id`도 바로 사용할 수 있습니다. 기존 service 없는 생성 요청도 동작합니다. 멱등키는 기존 클라이언트 호환을 위해 선택이지만 새 생성 화면에서는 꼭 보내세요.

같은 사용자·키·정규화된 본문은 동시 요청이나 응답 유실 후 재시도에도 같은 프로젝트를 반환합니다. 본문이 다르면 `409 IDEMPOTENCY_CONFLICT`입니다. 영수증은 프로젝트에 유지되며 메타데이터를 수정해도 재생성하지 않습니다. 휴지통 이동 후 같은 요청은 `409 PROJECT_DELETED`입니다. 영구 삭제 후에는 키도 사라집니다. 수정한 내용으로 새로 생성하려면 새 키를 사용하세요.

1. 사용자가 생성 모달을 작성하는 동안 값과 멱등키를 화면 상태에 보존합니다.
2. 저장 시 프로젝트·서비스를 함께 생성하고 반환받은 ID를 보존합니다.
3. 같은 서비스 ID에 `PUT /connection` → 필요한 경우 `PUT /connection/secret`을 호출합니다. 설정 저장 실패 시 프로젝트를 다시 만들지 않고 그 단계만 수정·재시도합니다. 기존 분리 생성 클라이언트에서 서비스 생성이 409이면 `GET /services`로 이미 등록된 서비스를 조회합니다.
4. 연결 테스트를 새 멱등키로 접수하고 Location의 상태를 조회합니다. `QUEUED/RUNNING`은 진행 중, `SUCCEEDED`만 성공, `FAILED`는 오류를 보여줍니다. HTTP 202는 연결 성공이 아닙니다.
5. 새로고침했다면 `GET /api/projects/{projectId}/services/{serviceId}/connection-tests`로 최신 테스트를 복구합니다. 미실행은 `data:null`입니다. `isCurrentRevision=false`이면 이전 설정의 결과이므로 최신 설정으로 다시 테스트합니다.

연결 설정·비밀값을 수정하면 revision이 증가하고 검증 상태가 초기화됩니다. 기존 결과 ID를 이미 알면 `/connection-tests/{id}`로 계속 조회할 수 있습니다. 실패는 자동으로 새 테스트를 만들지 않으며 사용자가 재시도할 때 새 멱등키로 접수합니다. 별도 connection worker가 실행되어야 큐가 처리됩니다.

## 키 관리와 네트워크

- 기업 챗봇 인증키: 서비스별 `/connection/secret`에서 암호화 저장합니다. 읽기는 `auth.secretConfigured`만 제공하며 원문을 반환하지 않습니다. 설정 URL·인증 방식이 바뀌면 기존 키를 제거합니다.
- 질문 생성·채점 모델의 제공사 인증: 서버 IAM role로 Bedrock을 호출합니다. 서비스 연결 화면에서 AWS 키나 팀원 개인 모델 API 키를 입력받지 않습니다. 기업 챗봇과 평가 AI는 별개의 연결입니다.
- 사용자 결정에 따라 이번 범위는 공개 HTTPS, 443 포트만 지원합니다. 피그마의 `.internal`은 연결 가능한 주소 예시로 사용하지 않습니다. 사설 IP·localhost·메타데이터 주소·내부 도메인·리다이렉트는 기존 차단을 유지합니다. 실제 접속 시 DNS 결과도 검사합니다.

이 변경은 API 제공이며 프론트 화면 구현·실제 기업 API 호출은 포함하지 않습니다. 로컬 작업만으로 서버에 배포되지는 않으며 main 병합 후 CI/CD 결과를 별도로 확인해야 합니다.

## #24와 병합 시 확인

인증 PR #32의 `20261105_11`과 번호가 겹치지 않도록 이번 마이그레이션은 `20261105_25`이며 현재 main의 `20261105_10` 다음입니다. 두 PR이 모두 스키마를 추가하므로 나중에 병합하는 PR에서 최신 main 기준으로 `down_revision`을 연결하거나 두 head의 병합 마이그레이션을 추가해야 합니다. 인증 PR을 이 브랜치에 섞지 않았습니다. 공통 응답 파일은 프로젝트 타입만 추가하며 사용자 응답은 수정하지 않습니다. 공통 테스트의 사용자 등록 helper도 변경하지 않습니다.
