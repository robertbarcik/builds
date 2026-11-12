#!/usr/bin/env bash
# Create the Python 3.12 venv in .venv and install the pinned requirements.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "python3.12 not found (set PYTHON=/path/to/python3.12)" >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv with $PY"
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Installed:"
.venv/bin/python -m pip freeze | grep -i -E '^(fastapi|uvicorn|openai|pydantic|pyyaml|pytest|httpx)=='

if [ ! -f .env ]; then
    echo "No .env yet: copy .env.example to .env and put your OPENROUTER_API_KEY in it."
fi
echo "Next: .venv/bin/python scripts/verify_models.py && .venv/bin/python scripts/build_history.py && ./run.sh"
