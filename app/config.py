"""Config and secrets loading. The API key is stripped and never logged or returned."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
HISTORY_PATH = DATA_DIR / "history.jsonl"
STATIC_DIR = ROOT / "static"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


def _read_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'").strip()
    return out


def openrouter_key() -> str:
    """Return the OpenRouter key (stripped) or an empty string. Never print the result."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        key = _read_env_file().get("OPENROUTER_API_KEY", "").strip()
    return key


def key_present() -> bool:
    return bool(openrouter_key())


def model_ids() -> dict[str, str]:
    return {role: spec["id"] for role, spec in load_config()["models"].items()}
