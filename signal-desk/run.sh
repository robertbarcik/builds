#!/usr/bin/env bash
# Start Signal Desk on the port from config.yaml (127.0.0.1:7870 by default).
#   ./run.sh            start, verify, then stay in the foreground (Ctrl-C stops it)
#   ./run.sh --detach   start, verify, return (server keeps running; log in logs/server.log)
set -euo pipefail
cd "$(dirname "$0")"

DETACH=0
if [ "${1:-}" = "--detach" ]; then DETACH=1; fi

if [ ! -x .venv/bin/python ]; then
    echo "No .venv yet: run ./setup.sh first." >&2
    exit 1
fi
PY=.venv/bin/python
HOST=$($PY -c "import yaml; print(yaml.safe_load(open('config.yaml'))['server']['host'])")
PORT=$($PY -c "import yaml; print(yaml.safe_load(open('config.yaml'))['server']['port'])")

if [ ! -f data/history.jsonl ]; then
    echo "Building data/history.jsonl (first run)..."
    $PY scripts/build_history.py >/dev/null
fi

# Stop an existing instance: only the process LISTENING on the port, never the clients
# connected to it (a plain `lsof -ti :PORT` also lists the browser).
existing=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$existing" ]; then
    echo "Stopping existing instance on port $PORT (pid $existing)..."
    kill $existing 2>/dev/null || true
    # Wait for the PROCESS to exit, not just the port: uvicorn closes the listener first and
    # would otherwise keep serving an open browser stream (and spending on AI calls).
    for _ in $(seq 1 40); do
        alive=0
        for p in $existing; do if kill -0 "$p" 2>/dev/null; then alive=1; fi; done
        if [ "$alive" = 0 ]; then break; fi
        sleep 0.2
    done
    for p in $existing; do
        if kill -0 "$p" 2>/dev/null; then
            echo "Old instance (pid $p) did not exit; killing it."
            kill -9 "$p" 2>/dev/null || true
        fi
    done
    if [ -n "$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)" ]; then
        echo "Port $PORT still busy; refusing to start a second instance." >&2
        exit 1
    fi
fi

BOOT_ID=$($PY -c "import uuid; print(uuid.uuid4().hex[:12])")
mkdir -p logs
echo "Starting Signal Desk on http://$HOST:$PORT (boot $BOOT_ID)"
SIGNAL_DESK_BOOT_ID="$BOOT_ID" nohup $PY -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
    --log-level warning --timeout-graceful-shutdown 2 >logs/server.log 2>&1 &
PID=$!

# A health answer only counts if it carries the boot id of THIS start (not a stale process).
ok=0
for _ in $(seq 1 50); do
    got=$(curl -s --max-time 1 "http://$HOST:$PORT/api/health" 2>/dev/null \
          | $PY -c "import json,sys; print(json.load(sys.stdin).get('boot_id',''))" 2>/dev/null || true)
    if [ "$got" = "$BOOT_ID" ]; then ok=1; break; fi
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 0.2
done
if [ "$ok" != 1 ]; then
    echo "Health check failed: no answer with boot_id $BOOT_ID. Last log lines:" >&2
    tail -n 20 logs/server.log >&2 || true
    kill "$PID" 2>/dev/null || true
    exit 1
fi
echo "Healthy: boot_id $BOOT_ID, pid $PID"

if [ "$DETACH" = 1 ]; then
    exit 0
fi
trap 'kill "$PID" 2>/dev/null || true' INT TERM
wait "$PID"
