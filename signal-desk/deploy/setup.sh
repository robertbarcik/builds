#!/usr/bin/env bash
# Configure a fresh Ubuntu 24.04 host (t3.small, 2 GB RAM) to serve Signal Desk behind Caddy
# (HTTPS + the app's login form). Every model call goes to OpenRouter; nothing runs locally.
#
# Run ON THE INSTANCE as root, from a checkout in /opt/signal-desk that already holds a .env:
#     HOST=3-70-1-2.sslip.io bash deploy/setup.sh
#
# .env must contain OPENROUTER_API_KEY, APP_PASSWORD (login "guest") and ADMIN_PASSWORD (login
# "robert"). Re-running is safe: every step is idempotent (the history is rebuilt only if missing).
set -euo pipefail

APP_DIR=/opt/signal-desk
HOST=${HOST:?set HOST to the public hostname, e.g. 3-70-1-2.sslip.io}

cd "$APP_DIR"
test -f .env || { echo ".env missing in $APP_DIR (copy it over with scp first)"; exit 1; }
envval() { grep -E "^$1=" .env | cut -d= -f2- | tr -d '"' || true; }
[ -n "$(envval OPENROUTER_API_KEY)" ] || { echo "OPENROUTER_API_KEY missing in .env"; exit 1; }
[ -n "$(envval APP_PASSWORD)" ]       || { echo "APP_PASSWORD missing in .env"; exit 1; }
[ -n "$(envval ADMIN_PASSWORD)" ]     || { echo "ADMIN_PASSWORD missing in .env"; exit 1; }
# the app's own login form (app/auth.py) is what protects the public URL
grep -qE "^LOGIN_ENABLED=" .env || echo "LOGIN_ENABLED=1" >> .env

echo "== swap (1 GB headroom for pip on 2 GB RAM)"
if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv caddy curl

echo "== app user + venv (Ubuntu 24.04 ships Python 3.12, the version the repo pins against)"
id -u desk >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin desk
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/state logs
chmod 600 .env

echo "== 30-day history (seeded, deterministic; ~10 s)"
[ -f data/history.jsonl ] || .venv/bin/python scripts/build_history.py >/dev/null
chown -R desk:desk "$APP_DIR"

echo "== systemd service"
install -m 644 deploy/signal-desk.service /etc/systemd/system/signal-desk.service
systemctl daemon-reload
systemctl enable --now signal-desk
systemctl restart signal-desk

echo "== caddy ($HOST)"
sed -e "s|{HOST}|$HOST|" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

sleep 3
systemctl --no-pager --lines=3 status signal-desk caddy | grep -E "Active|●" || true
# health answers 401 behind the login: that is the expected answer here
curl -s -o /dev/null -w 'local /api/health: HTTP %{http_code}\n' http://127.0.0.1:7870/api/health || true
echo
echo "== done: https://$HOST  (users: guest, robert)"
