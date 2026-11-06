# 대회 서버 Bedrock 연결

서버 `16.208.41.218`, 리전 `ap-northeast-3`의 IAM 역할로 호출한다.
Kiro 로그인 리전 `us-east-1`과 구분한다. AWS 액세스 키를 만들거나 개인 OpenAI 키를 넣지 않는다.
사용자가 서버에서 확인한 기본 연결은 boto3 1.43.124, `iam-role`, Haiku 응답 성공이다.
이 확인은 일반 대화 호출이며 우리 서비스의 구조화 판정 품질 검증과는 구분한다.

## 애플리케이션 설정

```dotenv
AWS_DEFAULT_REGION=ap-northeast-3
QUESTION_GENERATION_PROVIDER=bedrock
QUESTION_GENERATION_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0
EVALUATION_JUDGE_PROVIDER=bedrock
EVALUATION_JUDGE_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0
```

설정은 API와 평가 워커에 동일하게 적용한다. 리전 우선순위는 AWS_REGION → AWS_DEFAULT_REGION →
오사카 기본값이다. 판정 모델을 생략하면 Bedrock 질문 생성 모델을 사용하고, 그것도 없으면 Haiku를 사용한다.
제공사를 비워두면 비활성화된다. AWS 권한이 없는 로컬에서는 테스트용 전송 대역을 사용한다.

질문 생성과 판정은 서로 다른 모델을 선택할 수 있다. 이 연결부는 Converse의 시스템 지시문과
지정 도구 호출을 지원하는 모델을 요구한다. 다른 모델로 바꾸기 전 이 기능과 출력 토큰 제한을 확인한다.
AWS에서 제공하는 모델 목록에 있다고 해서 모든 모델의 도구 호출 형식과 제한이 같은 것은 아니다.
Bedrock 오류 시 개인 API로 자동 전환하거나 같은 유료 요청을 자동 재시도하지 않는다.

## 출력 검증

Converse toolConfig로 제출 도구 하나를 지정한다. 응답의 도구명과 개수를 확인하고 도구 입력 JSON을
Pydantic으로 검증한다. 실제 도구 실행은 없다. native structured outputs는 사용하지 않는다.
질문·기대 조건·정책 출처·중복·개수·필수/금지 구분과 판정의 질문/조건 ID·근거·점수·인용문 검증은 유지한다.
질문 생성은 연결 5초·응답 읽기 55초, 판정은 연결 5초·읽기 110초·워커 330초다.
입력은 512KiB, 응답은 2MiB이며 토큰 제한으로 잘린 출력은 완료 결과로 저장하지 않는다.

워커는 Bedrock 판정 어댑터의 문항 동시 처리 한도(1개)를 적용한다.
`EVALUATION_ITEM_CONCURRENCY=3`이어도 단일 평가·A/B 비교 모두 문항을 순차 처리하므로,
정상 진행 중인 다른 문항 때문에 `JUDGE_BUSY`가 발생하지 않는다. 대기 문항은 SDK 실행기에 미리 제출하지 않는다.

워커의 330초 제한은 판정 대기를 종료하며 실행 중인 SDK 스레드를 강제로 중단하지 않는다.
판정은 기본 asyncio 실행기와 분리된 전용 실행기를 사용하며, 프로세스당 미종료 SDK 호출은 최대 1개다.
시간 초과·취소 후에도 실제 호출이 끝날 때까지 슬롯을 유지한다. 이전 호출이 남아 슬롯이 차 있으면 새 SDK 요청을
제출하거나 대기열에 쌓지 않고 JUDGE_BUSY로 기록한다. 호출이 끝나면 슬롯을 반환해 다시 사용할 수 있다.
늦게 도착한 응답은 이미 저장한 시간 초과 결과를 덮어쓰지 않는다.

이미 실패로 저장된 판정은 배포 후 자동으로 바뀌지 않는다. 수정 버전 배포 후 새 평가를 접수하고
문항 상세의 `a.judgeError`(A/B 비교는 `b.judgeError`도)를 확인한다.

주요 오류는 GENERATION_/JUDGE_ 접두사의 AUTH_FAILED, RATE_LIMITED, TIMEOUT,
INVALID_REQUEST, UNAVAILABLE, INCOMPLETE, REFUSED, RESPONSE_TOO_LARGE다.
잘못된 생성 형식은 GENERATION_SCHEMA_INVALID이며 개수·유형·중복·기대 조건 오류는
GENERATION_COUNT_MISMATCH / GENERATION_CATEGORY_MISMATCH / GENERATION_DUPLICATE_QUESTIONS /
GENERATION_CONDITIONS_INVALID로 구분한다. 잘못된 판정 출력은 INVALID_JUDGE_OUTPUT이다.
기업 API 답변 성공과 평가 모델 실패는 별도로 집계하며 모델 오류를 임의 통과·미통과로 만들지 않는다.

## 배포

런타임 SDK는 requirements.lock에 포함하므로 운영 가상환경에 수동 pip install하지 않는다.
GitHub production Variables에서 다음 공개 설정을 선택적으로 지정한다.

| Variable | 비어 있을 때 기본값 |
| --- | --- |
| BEDROCK_REGION | ap-northeast-3 |
| BEDROCK_GENERATION_MODEL | global.anthropic.claude-haiku-4-5-20251001-v1:0 |
| BEDROCK_JUDGE_MODEL | global.anthropic.claude-haiku-4-5-20251001-v1:0 |

CI는 개인 키를 전달하지 않고 제한 SSH 명령으로 서버의 provider/model/region을 적용한다.
이전 개인 AI 키는 서버 환경에서 제거하되 DB·인증·기업 챗봇 암호화 설정은 보존한다.
모델·리전 설정이 바뀌면 API와 평가 워커를 함께 재시작한다. 운영자의 일반 셸 export만으로는
systemd 서비스에 반영되지 않는다. 운영 환경 파일을 통째로 교체하지 않는다.

배포 후 가상의 정책 1~3문항으로 질문 생성 → 검토·확정 → 단일 평가 → 오류 분석을 확인한다.
API의 평가 스냅샷에서 provider=bedrock, 올바른 model/region을 확인하고 JUDGE_NOT_CONFIGURED가 없는지 본다.
실제 의미적 판정 품질은 정책 근거와 답변을 사람이 대조한다. 모델 설정 변경 전 큐 작업은 새 설정으로
임의 처리하지 않으므로 필요하면 새 평가를 접수한다.

참고: [Converse API](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/converse.html),
[IAM 역할 인증](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html#iam-roles),
[Converse 도구 호출](https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-client-side.html).
