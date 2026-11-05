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

팀에서 지급한 PEM은 초기 SSH 접속에만 사용합니다. 그 내용을 출력하거나 GitHub에 업로드하지 않습니다. CI 전용 키는 서버 authorized_keys에서 `restrict,command="/usr/local/bin/osori-ci-entrypoint"`로 제한하며, `deploy {40자리 SHA} {runId} {attempt}` 외의 명령을 거부합니다. 일반 셸·포트 포워딩·PTY를 허용하지 않습니다. 이 키는 배포 코드를 변경할 수 있는 권한이 있으므로 production 환경과 main 쓰기 권한을 가진 팀원만 관리해야 합니다.

## 처음 구성할 때

먼저 서버 상태와 포트 사용 여부를 확인합니다. 저장소의 deploy 디렉터리를 서버에 복사하고 `sudo bash deploy/bootstrap.sh`를 한 번 실행합니다. Ubuntu 패키지·로컬 DB·서비스 사용자·배포 도구를 준비합니다. 기존 osori DB가 있는데 환경 파일이 없으면 자동으로 덮어쓰지 않고 중단합니다.

CI 전용 키의 공개 키만 ubuntu의 authorized_keys에 위 제한 옵션과 함께 등록하고, GitHub production 환경에 키와 호스트 키를 비밀값으로 등록합니다. 배포 워크플로에서는 런타임 비밀 설정과 팀 PEM이 아닌 별도 CI 키만 사용합니다. 서버 패키지는 bootstrap 이후 자동 배포마다 다시 설치하지 않습니다.

## 배포 동작과 실패 처리

1. 서버와 GitHub 양쪽에서 동시 배포를 직렬화합니다.
2. 테스트한 커밋의 git archive를 전송합니다. 압축 크기 50MiB·해제 크기 200MiB로 제한하고 경로 탈출·링크·특수 파일을 거부합니다.
3. 새 디렉터리의 가상환경에 해시로 고정한 requirements.lock을 설치합니다. 이 단계에서 기존 API는 계속 실행됩니다.
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
