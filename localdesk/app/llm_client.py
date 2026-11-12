import json
import logging
import re

from openai import OpenAI

from app.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    ROLES,
)

logger = logging.getLogger(__name__)

_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

_openai_client = None


def get_client() -> OpenAI:
    return _client


def get_model() -> str:
    return LLM_MODEL


def get_role_client(role: str):
    """Resolve a role name to (client, model).

    Roles with provider "openai" use the OpenAI API when a key is configured;
    otherwise they fall back to the agent's client (local Ollama or OpenRouter)
    so the whole system keeps working without an OpenAI key.
    """
    global _openai_client
    cfg = ROLES.get(role, {})
    if cfg.get("provider") == "openai":
        if OPENAI_API_KEY:
            if _openai_client is None:
                _openai_client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
            return _openai_client, cfg["model"]
        logger.warning(
            "Role %r wants OpenAI but OPENAI_API_KEY is not set — falling back to %s",
            role, LLM_MODEL,
        )
    return _client, LLM_MODEL


def voice_available() -> bool:
    return bool(OPENAI_API_KEY)


def chat_kwargs(model: str, max_tokens: int, temperature: float, *, tools: bool = False) -> dict:
    """Sampling kwargs compatible with the model's provider.

    The gpt-5/gpt-6 families reject `max_tokens` (want `max_completion_tokens`)
    and only support the default temperature; Ollama/OpenRouter models take
    the classic parameters.

    gpt-6 on /v1/chat/completions accepts function tools ONLY with
    `reasoning_effort: "none"` (verified 2026-09-30: any other effort → 400
    "use /v1/responses or set reasoning_effort to 'none'"). Only the luna tier
    supports "none"; sol/astra cannot call tools here at all — tool-using
    roles must stay on luna unless the call site moves to the Responses API.
    """
    if model.startswith("gpt-6"):
        kw = {"max_completion_tokens": max_tokens}
        if tools:
            kw["reasoning_effort"] = "none"
        return kw
    if model.startswith("gpt-"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens, "temperature": temperature}


def parse_json_loosely(content: str) -> dict:
    """Parse JSON from model output that may be wrapped in markdown fences,
    preceded by <think> blocks (Qwen 3), or surrounded by prose."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if "```" in content:
        parts = content.split("```")
        if len(parts) > 1:
            content = parts[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Last resort: first balanced {...} object in the text
    start = content.find("{")
    if start == -1:
        raise json.JSONDecodeError("no JSON object found", content, 0)
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(content[start:i + 1])
    raise json.JSONDecodeError("unbalanced JSON object", content, start)
