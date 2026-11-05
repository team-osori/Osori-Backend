# Osori Backend

Osori는 AI 챗봇의 프롬프트를 같은 질문과 기준으로 비교하고, 배포 전 정책에 어긋나는 응답을 확인할 수 있도록 돕는 서비스입니다. 백엔드는 FastAPI와 MySQL로 구성합니다.

## 실행

Python 3.11 이상과 MySQL 데이터베이스가 필요합니다. MySQL에 사용할 데이터베이스와 계정을 미리 만들고 `.env.example`을 복사해 접속 정보를 설정하세요.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
# .env의 DATABASE_URL을 수정합니다.
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

DB 변경은 Alembic 마이그레이션으로 적용합니다. 이전에 자동 생성한 테이블이 있는 개발 DB라면 마이그레이션 전에 스키마를 확인해야 합니다. API 문서는 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다.

## 팀 연동 안내

프로젝트·챗봇 API, 공통 오류, 다른 담당자가 재사용할 소유권 검사와 토큰 처리 규칙은 [API 인계 문서](docs/API_HANDOFF.md)에 정리했습니다.

정책 텍스트·규칙 버전과 사람이 검토·확정하는 테스트셋 기능은 [정책·테스트셋 인계](docs/POLICY_TESTSETS.md)를 참고하세요. 수동 문항 작성·검토·확정과 Bedrock 기반 질문 생성을 지원합니다. 대회 서버의 IAM 역할을 사용하며 설정은 [Bedrock 연동](docs/BEDROCK_SETUP.md)에 있습니다.

기업 챗봇 설정·인증정보 암호화·연결 테스트와 프롬프트 버전 관리는 [연동·프롬프트 인계 문서](docs/CONNECTION_PROMPT_HANDOFF.md)를 참고하세요. 연결 테스트에는 별도 `.venv/bin/python -m app.connection_worker` 실행이 필요합니다. 인증정보 저장 전 `.env`에 `CONNECTION_ENCRYPTION_KEY`를 설정합니다.

평가 실행·A/B 비교는 [평가 인계 문서](docs/EVALUATION_HANDOFF.md)를 참고하세요. 별도 `.venv/bin/python -m app.evaluation_worker` 실행이 필요하며, 개발 서버 배포는 Bedrock 판정을 함께 활성화합니다. 제공사를 설정하지 않은 로컬에서는 답변만 수집하고 판정은 판정 불가로 표시합니다. 평가 1회는 최대 200문항이며 통과율은 0~100 백분율입니다.

GitHub Actions는 PR에서 테스트하고 main 반영 시 테스트 통과 후 팀 서버의 **8000 포트**에 API·워커를 배포합니다. 초기 설정·비밀값·백업·복구 절차는 [배포 문서](docs/DEPLOYMENT.md)에 있습니다.

정책·질문 생성·기대 조건·검토 이력과 B 담당 연동 계약은 [A 기능 인계](docs/QUESTION_GENERATION_HANDOFF.md)를 참고하세요. 질문 생성과 평가 판정 모두 Bedrock을 지원하며, 기본 모델은 Claude Haiku 4.5입니다. 서버에서 API와 평가 워커에 동일한 리전·모델 설정을 적용합니다.

## 인증 API

`/api/auth` 아래에서 회원가입, 이메일 중복 확인, 로그인, 현재 사용자 조회, 토큰 갱신, 로그아웃을 제공합니다. 액세스 토큰은 `Authorization: Bearer <accessToken>` 헤더에 넣습니다. 갱신하면 기존 토큰 쌍이 폐기되고 새 토큰 쌍이 발급됩니다. 이미 사용된 리프레시 토큰을 다시 사용하면 탈취 가능성을 고려해 해당 세션의 최신 토큰도 폐기하므로 다시 로그인해야 합니다. 동시에 같은 리프레시 토큰을 중복 요청해도 같은 정책이 적용되므로 클라이언트는 갱신 요청을 하나로 합쳐야 합니다. 로그아웃은 해당 세션의 두 토큰을 폐기합니다. 토큰은 서버에서 해시로만 보관합니다. 이메일은 소문자로 통일하며 이름은 앞뒤 공백을 제거합니다. 액세스 토큰 30분, 리프레시 토큰 7일을 기본값으로 사용합니다. 세션은 로그인 후 최대 30일 또는 갱신 1,000회까지만 유지합니다. 리프레시 토큰을 갱신해도 최대 수명을 연장하지 않아 이력이 무제한 쌓이지 않습니다. 이 기본값은 환경 변수로 변경할 수 있습니다.

회원가입·로그인·토큰 갱신에는 DB 기반 호출 제한이 적용됩니다. 기본값은 각각 1분당 10회·10회·30회이며 환경 변수로 조정할 수 있습니다. 제한은 서버가 인식한 클라이언트 IP 기준이므로 프록시 배포 시 신뢰할 수 있는 프록시만 전달 IP를 설정하도록 구성해야 합니다. 토큰 및 호출 제한의 구체적인 정책은 팀에서 확인해야 합니다.

```bash
.venv/bin/python -m pytest
```

기본 테스트는 별도 SQLite 메모리 DB를 사용합니다. MySQL 통합 테스트는 테스트 데이터를 생성하므로 반드시 전용 임시 DB를 지정합니다.

```bash
OSORI_TEST_MYSQL_URL='mysql+pymysql://user:password@127.0.0.1:3306/osori_test?charset=utf8mb4' .venv/bin/python -m pytest -q tests/test_mysql_auth_integration.py
```

사용된 리프레시 토큰의 해시는 해당 세션이 삭제될 때 함께 삭제됩니다. 만료된 세션은 `.venv/bin/python -m app.maintenance`로 정리합니다. 자동 실행 예약은 별도로 구성해야 합니다. 프론트엔드: [Osori-Frontend](https://github.com/team-osori/Osori-Frontend)
