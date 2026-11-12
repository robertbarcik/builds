#!/usr/bin/env bash
# Configure a fresh Ubuntu 24.04 host (t3.small, 2 GB RAM) to serve LocalDesk behind Caddy
# (HTTPS + password). Ollama runs locally for the nomic-embed-text embeddings only; the desk
# agent stays in cloud mode (OpenRouter) and the minis/voice on OpenAI.
#
# Run ON THE INSTANCE as root, from a checkout in /opt/localdesk that already holds a .env:
#     HOST=3-70-1-2.sslip.io bash deploy/setup.sh
#
# .env must contain OPENROUTER_API_KEY, OPENAI_API_KEY, APP_PASSWORD (login "student")
# and ADMIN_PASSWORD (login "robert"). Re-running is safe: every step is idempotent.
set -euo pipefail

APP_DIR=/opt/localdesk
HOST=${HOST:?set HOST to the public hostname, e.g. 3-70-1-2.sslip.io}

cd "$APP_DIR"
test -f .env || { echo ".env missing in $APP_DIR (copy it over with scp first)"; exit 1; }
envval() { grep -E "^$1=" .env | cut -d= -f2- | tr -d '"' || true; }
APP_PASSWORD=$(envval APP_PASSWORD)
ADMIN_PASSWORD=$(envval ADMIN_PASSWORD)
[ -n "$APP_PASSWORD" ]   || { echo "APP_PASSWORD missing in .env"; exit 1; }
[ -n "$ADMIN_PASSWORD" ] || { echo "ADMIN_PASSWORD missing in .env"; exit 1; }
# the app's own login form (app/auth.py) is what protects the public URL
grep -qE "^LOGIN_ENABLED=" .env || echo "LOGIN_ENABLED=1" >> .env

echo "== swap (2 GB; pip + chromadb + ollama on 2 GB RAM need headroom)"
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv python3-dev build-essential caddy sqlite3 curl

echo "== ollama (embeddings only)"
if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl enable --now ollama
for _ in $(seq 1 30); do
    curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
done
ollama pull nomic-embed-text

echo "== app user + venv"
id -u desk >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin desk
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/db logs vectorstore
chmod 600 .env

echo "== seed DB + ingest knowledge base"
.venv/bin/python scripts/seed_db.py
.venv/bin/python scripts/ingest.py
chown -R desk:desk "$APP_DIR"

echo "== systemd service"
install -m 644 deploy/localdesk.service /etc/systemd/system/localdesk.service
systemctl daemon-reload
systemctl enable --now localdesk
systemctl restart localdesk

echo "== caddy ($HOST)"
sed -e "s|{HOST}|$HOST|" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

sleep 3
systemctl --no-pager --lines=3 status ollama localdesk caddy | grep -E "Active|●" || true
curl -s http://127.0.0.1:7860/api/status || true
echo
echo "== done: https://$HOST  (users: robert, student)"
