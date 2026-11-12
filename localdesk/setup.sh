#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== LocalDesk Setup ==="

# Create venv if not exists (Python 3.10+ required for the MCP server/CLI)
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    PY=python3
    for candidate in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" &>/dev/null; then PY="$candidate"; break; fi
    done
    echo "Using $($PY --version)"
    "$PY" -m venv venv
fi

echo "Activating venv and installing dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

# Ensure Ollama is running and has required models
echo "Pulling Ollama models..."
if command -v ollama &>/dev/null; then
    ollama pull nomic-embed-text 2>/dev/null || echo "Note: Could not pull nomic-embed-text. Make sure Ollama is running."
    ollama pull qwen3.5:4b 2>/dev/null || echo "Note: Could not pull qwen3.5:4b. Make sure Ollama is running."
else
    echo "Warning: Ollama not found. Install from https://ollama.com and re-run setup."
fi

# Seed database
echo "Seeding database..."
python scripts/seed_db.py

# Ingest knowledge base
echo "Ingesting knowledge base into ChromaDB..."
python scripts/ingest.py

echo ""
echo "=== Setup complete! ==="
echo "Run ./run.sh to start the application."
echo "Open http://localhost:7860 in your browser."
