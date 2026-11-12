"""Ask-the-audit meta-chat — a small agent over logs/audit.jsonl.

The system introspecting its own guardrail history: counts by trigger type,
blocked interactions, judge verdicts, daily summaries.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import AUDIT_LOG_PATH
from app.llm_client import chat_kwargs, get_role_client
from app.ops.metrics import guardrail_stats, record_llm_usage
from app.prompts.audit_chat_system import AUDIT_CHAT_SYSTEM_PROMPT
from app.tracing import tracer

logger = logging.getLogger(__name__)

router = APIRouter()

# session_id -> messages (separate from the main desk conversations)
_audit_sessions: dict = {}

MAX_TOOL_ROUNDS = 4


def _load_records(hours: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = []
    try:
        with open(AUDIT_LOG_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if datetime.fromisoformat(rec.get("timestamp", "")) >= cutoff:
                        records.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return records


def audit_stats(hours: int = 24) -> str:
    """Counts of guardrail triggers by type."""
    return json.dumps(guardrail_stats(hours=hours))


def list_flagged(hours: int = 24, limit: int = 10) -> str:
    """Interactions whose guardrails triggered or judge did not PASS."""
    out = []
    for rec in reversed(_load_records(hours)):
        verdict = (rec.get("judge_verdict") or {}).get("verdict", "PASS")
        triggers = rec.get("guardrail_triggers", [])
        if not triggers and verdict == "PASS":
            continue
        out.append({
            "timestamp": rec.get("timestamp", ""),
            "user_input": (rec.get("user_input_sanitized") or rec.get("user_input_original", ""))[:140],
            "triggers": triggers,
            "judge": verdict,
            "judge_reason": (rec.get("judge_verdict") or {}).get("reason", "")[:140],
        })
        if len(out) >= limit:
            break
    return json.dumps({"flagged": out, "hours": hours}, ensure_ascii=False)


def tool_usage(hours: int = 24) -> str:
    """Which desk tools were invoked and how often."""
    counts = {}
    total = 0
    for rec in _load_records(hours):
        total += 1
        for tc in rec.get("tool_calls", []):
            name = tc.get("name", "unknown")
            counts[name] = counts.get(name, 0) + 1
    return json.dumps({"interactions": total, "tool_calls": counts, "hours": hours})


AUDIT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "audit_stats",
            "description": "Counts of guardrail triggers by type (injection, PII, SLA hallucination, judge flags/blocks, voice bypasses) over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer", "description": "Lookback window in hours (default 24)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_flagged",
            "description": "List recent interactions where a guardrail triggered or the judge flagged/blocked, with the trigger details and judge reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Lookback window in hours (default 24)"},
                    "limit": {"type": "integer", "description": "Max records (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_usage",
            "description": "Counts of desk tool invocations (search_kb, check_sla, create_incident, ...) over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer", "description": "Lookback window in hours (default 24)"}},
                "required": [],
            },
        },
    },
]

# Small models sometimes invent extra kwargs — keep only the declared ones,
# and coerce to int so "24" doesn't crash timedelta.
def _clean(args: dict, allowed: dict) -> dict:
    out = {}
    for key, default in allowed.items():
        try:
            out[key] = int(args.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


AUDIT_TOOL_HANDLERS = {
    "audit_stats": lambda args: audit_stats(**_clean(args, {"hours": 24})),
    "list_flagged": lambda args: list_flagged(**_clean(args, {"hours": 24, "limit": 10})),
    "tool_usage": lambda args: tool_usage(**_clean(args, {"hours": 24})),
}


def _run_audit_chat(message: str, session_id: str) -> dict:
    if session_id not in _audit_sessions:
        _audit_sessions[session_id] = [
            {"role": "system", "content": AUDIT_CHAT_SYSTEM_PROMPT}
        ]
    messages = _audit_sessions[session_id]
    messages.append({"role": "user", "content": message})

    client, model = get_role_client("audit_chat")
    all_tool_calls = []

    with tracer.start_as_current_span(
        "audit_chat", attributes={"mu.session_id": session_id}
    ):
        for _ in range(MAX_TOOL_ROUNDS):
            with tracer.start_as_current_span(
                "gen_ai.chat",
                attributes={
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": model,
                    "gen_ai.operation.name": "chat",
                    "mu.llm_call_type": "audit_chat",
                },
            ) as span:
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=AUDIT_TOOLS,
                    **chat_kwargs(model, max_tokens=900, temperature=0.1, tools=True),
                )
                duration = time.monotonic() - t0
                span.set_attribute("mu.llm_duration_s", round(duration, 3))
                if resp.usage:
                    record_llm_usage(
                        "audit_chat", model,
                        resp.usage.prompt_tokens or 0,
                        resp.usage.completion_tokens or 0,
                        duration, session_id=session_id,
                    )
            choice = resp.choices[0]
            if choice.message.tool_calls:
                messages.append(choice.message.model_dump())
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    handler = AUDIT_TOOL_HANDLERS.get(tc.function.name)
                    try:
                        result = handler(args) if handler else json.dumps({"error": "unknown tool"})
                    except Exception as e:
                        result = json.dumps({"error": str(e)})
                    all_tool_calls.append({"name": tc.function.name, "arguments": args})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                break

    answer = choice.message.content or "I could not get a clear answer from the audit log."
    messages.append({"role": "assistant", "content": answer})
    if len(messages) > 30:
        _audit_sessions[session_id] = [messages[0]] + messages[-12:]
    return {"response": answer, "tool_calls": all_tool_calls}


@router.post("/api/audit-chat")
async def audit_chat_endpoint(request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id", "audit-default")
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    try:
        return await asyncio.to_thread(_run_audit_chat, message, session_id)
    except Exception as e:
        logger.warning("Audit chat failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)
