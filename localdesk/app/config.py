import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env file if present
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

_config_path = PROJECT_ROOT / "config.yaml"

with open(_config_path) as f:
    _raw = yaml.safe_load(f)


def _expand(val: str) -> str:
    if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
        return os.environ.get(val[2:-1], "")
    return val


MODE: str = _raw.get("mode", "local")

_mode_cfg = _raw.get(MODE, {})
LLM_BASE_URL: str = _expand(_mode_cfg.get("base_url", ""))
LLM_MODEL: str = _expand(_mode_cfg.get("model", ""))
LLM_API_KEY: str = _expand(_mode_cfg.get("api_key", "not-needed"))

_openai_cfg = _raw.get("openai", {})
OPENAI_BASE_URL: str = _openai_cfg.get("base_url", "https://api.openai.com/v1")
OPENAI_API_KEY: str = _expand(_openai_cfg.get("api_key", ""))

ROLES: dict = _raw.get("roles", {})

# Voice channel: "live" = gpt-live-1 with client delegation (tools run through
# the guardrail pipeline while the model keeps talking); "realtime" = the older
# gpt-realtime path (browser bridge, guardrails bypassed). Switchable at runtime.
_voice_cfg = _raw.get("voice", {})
VOICE_MODE_DEFAULT: str = _voice_cfg.get("mode", "live")
VOICE_LIVE_MODEL: str = _voice_cfg.get("live_model", "gpt-live-1")
VOICE_REALTIME_MODEL: str = _voice_cfg.get("realtime_model", "gpt-realtime-2.1-mini")
VOICE_TRANSCRIPTION_MODEL: str = _voice_cfg.get("transcription_model", "gpt-live-transcribe")
VOICE_NAME: str = _voice_cfg.get("voice", "marin")

_sim_cfg = _raw.get("simulation", {})
SENTINEL_CADENCE_S: int = _sim_cfg.get("sentinel_cadence_s", 25)
MAX_EVENTS_PER_MIN: int = _sim_cfg.get("max_events_per_min", 12)

EMBEDDING_MODEL: str = _raw["embedding"]["model"]
EMBEDDING_BASE_URL: str = _raw["embedding"]["base_url"]

VECTORSTORE_PATH: str = str(PROJECT_ROOT / _raw["vectorstore"]["path"])
VECTORSTORE_COLLECTION: str = _raw["vectorstore"]["collection"]

DATABASE_PATH: str = str(PROJECT_ROOT / _raw["database"]["path"])

AUDIT_LOG_PATH: str = str(PROJECT_ROOT / _raw["audit"]["path"])

SERVER_HOST: str = _raw["server"]["host"]
SERVER_PORT: int = _raw["server"]["port"]
