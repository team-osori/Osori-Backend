#!/usr/bin/env bash
# Receives a git archive on stdin. This script is installed root-owned by bootstrap.
set -Eeuo pipefail
umask 027
[[ $EUID -eq 0 ]] || { echo 'Run through the deployment entrypoint.' >&2; exit 1; }
[[ $# -eq 3 && $1 =~ ^[a-f0-9]{40}$ && $2 =~ ^[1-9][0-9]{0,19}$ && $3 =~ ^[1-9][0-9]{0,19}$ ]] || exit 2
revision=$1
release_id="$1-$2-$3"
root=/opt/osori-backend
release="$root/releases/$release_id"
[[ -f /etc/osori-backend.env && -d "$root/releases" ]] || { echo 'Run bootstrap first.' >&2; exit 1; }
exec 9>/var/lock/osori-backend-deploy.lock
flock -w 900 9
[[ ! -e "$release" ]] || { echo 'This release attempt already exists; rerun with a new attempt.' >&2; exit 1; }
previous=$(readlink -f "$root/current" || true)
[[ -d "$previous" ]] || previous=''
temporary=$(mktemp -d /var/tmp/osori-release.XXXXXX)
services_stopped=0

install_units() {
  local source=$1
  install -m 0644 "$source/deploy/systemd/osori-api.service" /etc/systemd/system/osori-api.service
  install -m 0644 "$source/deploy/systemd/osori-worker.service" /etc/systemd/system/osori-worker.service
  systemctl daemon-reload
}

set_current() {
  ln -sfn "$1" "$root/current.next"
  mv -Tf "$root/current.next" "$root/current"
}

finish() {
  local status=$?
  trap - EXIT
  if [[ $status -ne 0 && $services_stopped -eq 1 ]]; then
    echo 'Deployment failed; restoring the previous application release.' >&2
    systemctl stop osori-api osori-worker || true
    if [[ -n "$previous" ]]; then
      set_current "$previous"
      install_units "$previous"
      systemctl reset-failed osori-api osori-worker || true
      systemctl start osori-api osori-worker || true
    fi
    echo 'Database migrations are not automatically reversed. A pre-migration backup is retained.' >&2
  fi
  rm -rf -- "$temporary"
  exit "$status"
}
trap finish EXIT

python3 /usr/local/lib/osori/archive.py "$temporary"
mv "$temporary/source" "$release"
chown -R osori:osori "$release"
chmod -R u+rwX,g+rX,o-rwx "$release"
cd "$release"
runuser -u osori -- python3.12 -m venv .venv
runuser -u osori -- .venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock
runuser -u osori -- .venv/bin/python -m pip check
runuser -u osori -- .venv/bin/python deploy/manage.py preflight
printf 'OSORI_RELEASE=%s\n' "$revision" > .release.env
chown -R root:osori "$release"
chmod -R g+rX,o-rwx "$release"

if [[ -z "$previous" ]] && ss -lntH 'sport = :8000' | grep -q .; then
  echo 'Port 8000 is already used by another application.' >&2
  exit 1
fi
services_stopped=1
if [[ -n "$previous" ]]; then
  # The worker handles SIGTERM by finishing the current bounded request before exiting.
  systemctl stop osori-worker osori-api
fi
.venv/bin/python deploy/manage.py backup "/var/backups/osori-backend/$release_id.sql.gz"
runuser -u osori -- .venv/bin/python deploy/manage.py migrate
set_current "$release"
install_units "$release"
systemctl enable osori-api osori-worker
systemctl reset-failed osori-api osori-worker || true
systemctl start osori-api osori-worker

ready=0
for _ in {1..45}; do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8000/api/ready \
      | .venv/bin/python -c 'import json,sys; assert json.load(sys.stdin) == {"status":"ok","revision":sys.argv[1]}' "$revision" 2>/dev/null \
      && systemctl is-active --quiet osori-worker; then
    ready=1
    break
  fi
  sleep 1
done
[[ $ready -eq 1 ]] || { echo 'Release readiness check failed.' >&2; exit 1; }
services_stopped=0
if [[ -n "$previous" ]]; then
  ln -sfn "$previous" "$root/previous"
fi
# Apply deploy-tool updates only after the new release is healthy.
install -m 0755 deploy/deploy.sh /usr/local/sbin/osori-deploy
install -m 0755 deploy/ssh_entrypoint.py /usr/local/bin/osori-ci-entrypoint
install -m 0644 deploy/archive.py /usr/local/lib/osori/archive.py

python3 - "$root" <<'PY'
from pathlib import Path
import re
import shutil
import sys

root = Path(sys.argv[1])
protected = {p.resolve() for p in (root / 'current', root / 'previous') if p.exists()}
releases = sorted((p for p in (root / 'releases').iterdir()
                   if p.is_dir() and not p.is_symlink() and re.fullmatch(r'[a-f0-9]{40}-[0-9]+-[0-9]+', p.name)),
                  key=lambda p: p.stat().st_mtime, reverse=True)
for release in releases[5:]:
    if release.resolve() not in protected:
        shutil.rmtree(release)
backups = sorted(Path('/var/backups/osori-backend').glob('*.sql.gz'),
                 key=lambda p: p.stat().st_mtime, reverse=True)
for backup in backups[3:]:
    backup.unlink()
PY
echo "Deployed $revision on port 8000; API and worker are healthy."
