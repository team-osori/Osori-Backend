#!/usr/bin/env bash
# One-time Ubuntu 24.04 host setup. Does not read or install the team's PEM key.
set -Eeuo pipefail
umask 027
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12-venv mysql-server curl ca-certificates

if ! id osori >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/osori-backend --create-home --shell /usr/sbin/nologin osori
fi
install -d -m 0750 -o root -g osori /opt/osori-backend /opt/osori-backend/releases
install -d -m 0700 /var/backups/osori-backend
install -d -m 0755 /usr/local/lib/osori
install -m 0755 "$script_dir/deploy.sh" /usr/local/sbin/osori-deploy
install -m 0755 "$script_dir/ssh_entrypoint.py" /usr/local/bin/osori-ci-entrypoint
install -m 0644 "$script_dir/archive.py" /usr/local/lib/osori/archive.py
/usr/local/sbin/osori-deploy --prepare-host

if [[ ! -f /etc/osori-backend.env ]]; then
  # This host was inspected before bootstrap; fail closed if an unexpected DB already exists.
  systemctl start mysql
  if [[ $(mysql --batch --skip-column-names -e "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='osori'") != 0 ]]; then
    echo 'An Osori database already exists without an environment file; configure it manually.' >&2
    exit 1
  fi
  /usr/bin/python3 -I - <<'PY'
import base64
import os
from pathlib import Path
import secrets
import subprocess

password = secrets.token_urlsafe(32)
key = base64.urlsafe_b64encode(os.urandom(32)).decode()
sql = (
    'CREATE DATABASE osori CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;'
    f"CREATE USER 'osori_app'@'127.0.0.1' IDENTIFIED BY '{password}';"
    "GRANT ALL PRIVILEGES ON osori.* TO 'osori_app'@'127.0.0.1';"
)
result = subprocess.run(['mysql'], input=sql, text=True, capture_output=True)
if result.returncode:
    raise SystemExit('Database provisioning failed; inspect the local database before retrying.')
env = Path('/etc/osori-backend.env')
with env.open('x') as target:
    target.write(f'DATABASE_URL=mysql+pymysql://osori_app:{password}@127.0.0.1:3306/osori?charset=utf8mb4\n')
    target.write(f'CONNECTION_ENCRYPTION_KEY={key}\n')
    target.write('CORS_ORIGINS=http://localhost:5173,http://16.208.41.218:5173,http://16.208.41.218:3000\n')
env.chmod(0o640)
PY
  chown root:osori /etc/osori-backend.env
fi

# Keep database access on loopback despite the broad hackathon security group.
cat > /etc/mysql/mysql.conf.d/osori-network.cnf <<'EOF'
[mysqld]
bind-address=127.0.0.1
mysqlx-bind-address=127.0.0.1
EOF
systemctl enable mysql
systemctl restart mysql
echo 'Host ready. Environment credentials are stored only in /etc/osori-backend.env.'
