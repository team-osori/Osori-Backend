# GitHub Actions 배포

## 배포 흐름

`.github/workflows/backend.yml`은 main 대상 PR과 main push에서 Python 3.12 테스트를 실행합니다. 일반 API 테스트, MySQL 8.0 동시성 테스트, Alembic 스키마 차이 검사, 배포 스크립트 검사를 수행합니다. PR에서는 배포 비밀값을 사용하지 않습니다.

main의 테스트가 성공하면 production 환경에서 SSH로 해당 커밋을 배포합니다. main보다 오래된 실행은 배포하지 않습니다. 수동 실행도 Actions의 Backend CI/CD → Run workflow에서 **main**을 선택하며 테스트부터 실행합니다.

배포 대상은 `ubuntu@16.208.41.218`, API는 `0.0.0.0:8000`입니다.

- Swagger: http://16.208.41.218:8000/docs
- 프로세스 확인: http://16.208.41.218:8000/api/health
- DB 연결·배포 커밋 확인: http://16.208.41.218:8000/api/ready

현재 IP 기반 HTTP 구성입니다. 도메인·HTTPS 역방향 프록시는 별도 설정입니다. MySQL과 MySQL X 포트는 127.0.0.1에만 바인딩하며 외부 공개하지 않습니다. 기존 8001 포트 서비스와 AWS 인스턴스 역할은 변경하지 않습니다. AWS 액세스 키는 생성하거나 저장하지 않습니다.

## 서버 구성

| 항목 | 위치/설명 |
| --- | --- |
| API 서비스 | `osori-api.service`, Uvicorn 프로세스 2개, 포트 8000 |
| 연결 테스트 워커 | `osori-worker.service`, API와 별도 프로세스 |
| A/B 평가 워커 | `osori-evaluation-worker.service`, API와 별도 프로세스 |
| Python | Ubuntu 24.04의 Python 3.12, 릴리스별 `.venv` |
| MySQL | 로컬 `osori` DB, 전용 `osori_app` 계정 |
| 애플리케이션 사용자 | 로그인 불가능한 시스템 사용자 `osori` |
| 빌드 사용자 | 별도 시스템 사용자 `osori-build`, runtime과 그룹·홈 분리 |
| 현재/직전 릴리스 | `/opt/osori-backend/current`, `/opt/osori-backend/previous` 심볼릭 링크 |
| 릴리스 저장 | `/opt/osori-backend/releases/{commit}-{runId}-{attempt}` |
| 공통 비밀 설정 | `/etc/osori-backend.env`, root:osori, 640 |
| DB 백업 | `/var/backups/osori-backend/*.sql.gz`, root 전용, 최근 3개 유지 |

`DATABASE_URL`, `CONNECTION_ENCRYPTION_KEY`, `CORS_ORIGINS`는 서버 환경 파일에서 관리합니다. 초기 bootstrap이 DB 비밀번호·암호화 키를 임의 생성하고 파일에만 저장하며 로그에 출력하지 않습니다. 기존 환경 파일은 덮어쓰지 않습니다. 프론트엔드 주소가 정해지면 CORS_ORIGINS를 수정하고 API 서비스를 재시작하세요. 암호화 키를 임의로 바꾸면 기존 챗봇 비밀값을 복호화하지 못하므로 보존해야 합니다.


## GitHub production 환경

production 환경은 main 브랜치의 배포만 허용합니다.

| 종류 | 이름 | 값/용도 |
| --- | --- | --- |
| Variable | DEPLOY_HOST | `16.208.41.218` |
| Variable | DEPLOY_USER | `ubuntu` |
| Secret | DEPLOY_SSH_KEY | GitHub Actions 전용 SSH 개인 키 |
| Secret | DEPLOY_KNOWN_HOSTS | 최초 SSH 연결에서 확인한 서버 공개 호스트 키 |

팀에서 지급한 PEM은 초기 SSH 접속에만 사용합니다. 그 내용을 출력하거나 GitHub에 업로드하지 않습니다. CI 전용 키는 서버 authorized_keys에서 `restrict,command="/usr/local/bin/osori-ci-entrypoint"`로 제한하며, `deploy {40자리 SHA} {runId} {attempt}`와 `configure-question-generation` 외의 명령을 거부합니다. 일반 셸·포트 포워딩·PTY를 허용하지 않습니다. 이 키는 배포 코드를 변경할 수 있는 권한이 있으므로 production 환경과 main 쓰기 권한을 가진 팀원만 관리해야 합니다.

## 처음 구성할 때

먼저 서버 상태와 포트 사용 여부를 확인합니다. 저장소의 deploy 디렉터리를 서버에 복사하고 `sudo bash deploy/bootstrap.sh`를 한 번 실행합니다. Ubuntu 패키지·로컬 DB·서비스 사용자·배포 도구를 준비합니다. 기존 osori DB가 있는데 환경 파일이 없으면 자동으로 덮어쓰지 않고 중단합니다.

CI 전용 키의 공개 키만 ubuntu의 authorized_keys에 위 제한 옵션과 함께 등록하고, GitHub production 환경에 키와 호스트 키를 비밀값으로 등록합니다. 배포 워크플로는 팀 PEM 대신 별도 CI 키를 사용하며, 질문 생성 전용 Secret을 별도 제한 명령의 표준입력으로 전달합니다. 서버 패키지는 bootstrap 이후 자동 배포마다 다시 설치하지 않습니다.

## 배포 동작과 실패 처리

1. 서버와 GitHub 양쪽에서 동시 배포를 직렬화합니다.
2. 테스트한 커밋의 git archive를 전송합니다. 압축 크기 50MiB·해제 크기 200MiB로 제한하고 경로 탈출·링크·특수 파일을 거부합니다.
3. 새 디렉터리의 가상환경에 별도 osori-build 계정으로 해시 고정 requirements.lock을 설치합니다. 소스·배포 코드는 처음부터 root 소유·그룹 읽기 전용이며 빌드는 .venv만 쓸 수 있습니다. 이 단계에서 기존 API는 계속 실행됩니다.
4. 환경 설정·암호화 키·DB 연결을 확인합니다.
5. 워커의 진행 중 호출이 끝나면 API와 워커를 멈추고 DB 백업을 생성합니다. 백업 실패 시 마이그레이션하지 않습니다.
6. Alembic upgrade head 후 current 링크와 systemd 설정을 갱신하고 API·워커를 시작합니다.
7. 내부 readiness 응답의 커밋 SHA와 워커 상태를 확인합니다. GitHub에서도 공개 8000 포트의 readiness를 확인합니다.
8. 최근 릴리스 5개와 현재·직전 릴리스를 보존하고 오래된 파일을 정리합니다.

실패하면 이전 애플리케이션 링크·서비스 설정으로 복구합니다. **DB 마이그레이션은 자동으로 역적용하지 않습니다.** 이전 코드와 호환되는 확장형 마이그레이션을 기본으로 작성해야 합니다. 호환되지 않는 스키마 변경은 운영자가 백업과 변경 내용을 검토해 복원해야 합니다. 서비스 재시작 구간에는 짧은 중단이 있습니다.

## 확인 명령

```bash
sudo systemctl status osori-api osori-worker osori-evaluation-worker --no-pager
sudo journalctl -u osori-api -u osori-worker -u osori-evaluation-worker -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/api/ready
readlink -f /opt/osori-backend/current
```

환경 파일·SSH 개인 키·DB 덤프 원문을 로그나 채팅에 출력하지 마세요. 배포 실패 후 GitHub 실행 로그와 journal의 오류 종류를 확인합니다. 서버 환경을 수정한 뒤 Actions에서 main 워크플로를 다시 실행할 수 있습니다.

### 사전 검사 실패 복구

사전 검사는 필수 설정 이름과 고정된 안내만 출력하며 환경변수 값이나 DB 드라이버의 예외 원문은 출력하지 않습니다. OCR 실행 파일/언어팩·DB URL·암호화 키·DB 연결 실패를 구분합니다. 예상하지 못한 예외는 기존처럼 종류만 출력합니다.

OCR 도입 이전부터 운영하던 서버는 다음 패키지를 한 번 설치해야 합니다. 코드 배포는 OS 패키지를 자동 설치하지 않습니다.

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng
tesseract --list-langs  # eng, kor 포함 여부 확인
```


## 의존성 갱신

pyproject.toml을 변경하면 잠금 파일도 함께 갱신하고 테스트합니다.

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements-dev.lock
```

참고: [GitHub Actions 비밀값](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets), [동시 실행 제어](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency), [systemd 서비스](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

## 평가 워커 최초 도입 시

평가 워커 unit은 저장소에 추가했으며 실제 운영 서버 반영 여부는 별도로 확인해야 합니다. 기존 서버는 직전 릴리스에서 설치한 배포 도구를 사용하므로, 평가 워커가 포함된 첫 배포 **전에** 운영자가 이번 변경의 deploy 디렉터리에서 아래 도구를 갱신해야 합니다. API 코드나 환경 파일은 이 명령으로 교체하지 않습니다.

```bash
sudo install -m 0755 deploy/deploy.sh /usr/local/sbin/osori-deploy
sudo install -m 0644 deploy/archive.py /usr/local/lib/osori/archive.py
```

이후 새 릴리스 배포가 API·연결 테스트 워커·평가 워커의 unit을 설치하고 함께 시작·확인합니다. 평가 워커 종료 대기 한도는 480초이며 현재 문항의 A/B 기업 호출·판정이 끝나도록 기다립니다. 남은 문항은 WORKER_STOPPED로 끝나므로 배포 전에 진행 중 평가를 확인하세요. 이전 코드로 롤백하면 이전 릴리스에 평가 unit이 있는지 확인해 서비스를 복구하며, 평가 기능 이전 릴리스라면 평가 서비스를 중지·해제합니다. DB 테이블과 저장된 평가 이력은 자동 삭제하지 않습니다.

## 배포 권한 분리

운영 API·워커는 `osori`, 의존성 빌드는 `osori-build`로 실행합니다. 두 계정은 그룹·홈을 공유하지 않습니다. 빌드 홈 `/var/lib/osori-build`는 root 소유 750이며 런타임 계정은 접근할 수 없습니다. `/opt/osori-backend`와 releases 부모에는 빌드 계정이 경로를 통과하도록 others 실행 권한만 부여하고, 개별 릴리스·파일은 별도 그룹 권한으로 보호합니다.

빌드 프로세스는 비운 환경(`env -i`)·Python 격리 모드(`-I`)·pip `--isolated --no-cache-dir`를 사용합니다. 운영 홈의 user site·sitecustomize·pip 설정을 읽지 않습니다. 원본 소스는 빌드 동안 root 소유로 유지하며 쓰기 가능한 `.venv`만 빌드 계정에 줍니다. 빌드가 끝나면 릴리스 전체를 root:osori의 읽기 전용 파일로 고정하고 preflight·마이그레이션을 운영 계정으로 실행합니다.

root 백업은 `/usr/bin/python3 -I deploy/backup.py <백업경로>`로 실행합니다. 이 코드는 root 소유 원본이며 표준 라이브러리만 사용합니다. 앱 가상환경·앱 패키지·운영 계정의 Python 시작 코드를 root로 실행하지 않습니다. 백업은 기존 서버 환경 파일의 로컬 osori DB만 허용하며 결과는 600 권한의 새 파일로 생성합니다. 실패한 부분 파일만 삭제하고 기존 백업을 덮어쓰지 않습니다.

새 bootstrap은 빌드 계정도 준비하며, 이미 구성한 서버에서는 새 배포 스크립트가 첫 사용 시 필요한 계정·권한을 확인하고 준비합니다. 기존의 root 설치 배포 도구를 **첫 보안 패치 배포부터** 적용하려면 신뢰할 수 있는 관리자 세션에서 최신 deploy 디렉터리로 다음을 실행합니다.

```bash
sudo install -m 0755 deploy/deploy.sh /usr/local/sbin/osori-deploy
sudo install -m 0644 deploy/archive.py /usr/local/lib/osori/archive.py
sudo /usr/local/sbin/osori-deploy --prepare-host
```

서버 도구를 선갱신하지 않으면 기존 도구가 이번 릴리스를 배포하고, 성공 후 새 도구를 설치하므로 권한 분리는 그 다음 배포부터 적용됩니다. 실제 서버의 계정·도구 갱신 여부는 별도로 확인해야 합니다. `deploy/manage.py backup`은 이전 배포 도구와의 한 차례 호환을 위한 경로이며 신규 root 배포에서는 사용하지 않습니다.

GitHub Actions는 읽기 전용 저장소를 붙인 일회용 Python 컨테이너에서 운영 홈·PYTHONPATH·앱 가상환경 오염과 원본 쓰기 시도를 검증합니다. 직접 확인할 때도 운영 서버가 아닌 일회용 컨테이너를 사용합니다.

```bash
docker run --rm --network none -v "$PWD:/source:ro" \
  python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 \
  python -I /source/tests/deployment_isolation_check.py
```

## Bedrock 질문 생성·평가 판정

대회 서버 IAM 역할을 사용한다. AWS 액세스 키나 개인 AI API 키를 GitHub에 추가할 필요가 없다.
production 환경의 공개 Variables는 BEDROCK_REGION, BEDROCK_GENERATION_MODEL, BEDROCK_JUDGE_MODEL이다.
비어 있으면 ap-northeast-3 및 Claude Haiku 4.5를 사용한다. 전체 설정은 [Bedrock 연동](BEDROCK_SETUP.md)에 있다.

제한 SSH 명령 configure-question-generation은 기존 명칭을 유지하지만 생성과 판정을 함께 설정한다.
root 소유 deploy/question_generation_env.py가 리전과 두 모델 ID만 검증하고
/etc/osori-backend.env를 원자적으로 갱신한다. 기존 DB·기업 챗봇 암호화 설정은 보존하고
개인 OPENAI_API_KEY·QUESTION_GENERATION_API_KEY·EVALUATION_JUDGE_API_KEY는 제거한다.
파일 권한은 root:osori 640이며 값이 바뀐 경우 API와 평가 워커를 함께 재시작한다.

설정 동기화는 새 릴리스 설치 후에 실행된다. 잘못된 리전·모델 형식이나 helper 실패는 Actions 실패로 표시한다.
검증·파일 갱신 실패 시 이전 설정은 보존되고 이미 배포한 앱은 유지된다. 파일 반영 후 서비스 재시작이
실패하면 새 설정은 남고 Actions는 실패하므로 서비스 상태를 확인한다. 공개 readiness는 DB·릴리스·프로세스
상태 확인이며 실제 Bedrock 호출 권한 및 모델 응답 품질까지 증명하지 않는다.
AWS_REGION과 AWS_DEFAULT_REGION을 서비스 환경에 동일하게 적용하므로 SSH 사용자 홈의 AWS 설정에 의존하지 않는다.
전환 전 큐에 남은 OpenAI 설정의 평가는 다른 모델로 재실행하지 않고 EXECUTION_CONFIG_UNAVAILABLE로 종료하므로
새 Bedrock 평가를 접수한다. 과거 스냅샷과 결과는 변경하지 않는다.
