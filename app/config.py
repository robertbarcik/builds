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


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def public_mode() -> bool:
    """SIGNAL_DESK_PUBLIC=1 (set by the deploy kit's systemd unit): tighter caps, no dev hooks."""
    return _flag("SIGNAL_DESK_PUBLIC")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        cfg = yaml.safe_load(fh)
    if public_mode():
        cfg.setdefault("caps", {}).update((cfg.get("public") or {}).get("caps") or {})
    return cfg


def dev_rules_enabled() -> bool:
    """POST /api/rules (manual rule creation, a developer aid for scripts/try_rule.py) is off unless
    config.yaml `dev.manual_rules` or SIGNAL_DESK_DEV=1 turns it on; never on a public deploy."""
    if public_mode():
        return False
    return _flag("SIGNAL_DESK_DEV") or bool((load_config().get("dev") or {}).get("manual_rules", False))


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


def env_value(name: str) -> str:
    """A setting from the environment, else from .env (stripped). Never print secrets read here."""
    v = os.environ.get(name, "").strip().strip('"').strip("'")
    return v or _read_env_file().get(name, "").strip()


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
