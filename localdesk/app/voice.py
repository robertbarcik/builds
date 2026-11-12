"""Voice mode — OpenAI Realtime (GA) session minting + tool bridge.

The browser talks WebRTC directly to OpenAI using a short-lived client secret
minted here. Tool calls raised on the Realtime data channel are executed by
this bridge through the same TOOL_HANDLERS as the chat pipeline, and every
voice turn is audit-logged with an explicit "guardrails bypassed" marker —
the realtime audio path skips the 3-layer text pipeline, which is a deliberate
teaching point of this demo.
"""

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import (
    OPENAI_API_KEY,
    VOICE_NAME,
    VOICE_REALTIME_MODEL,
    VOICE_TRANSCRIPTION_MODEL,
)
from app.guardrails.audit import log_interaction
from app.tools.definitions import TOOLS
from app.ws_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

VOICE_MODEL = VOICE_REALTIME_MODEL


# Screen-control tools executed by the BROWSER, not this backend — they let
# the caller say "pull up the dashboard" and watch the UI respond.
UI_TOOLS = [
    {
        "type": "function",
        "name": "show_dashboard",
        "description": "Open the incident dashboard panel on the user's screen (counts by status/priority, recent tickets).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "show_monitoring",
        "description": "Open the live monitoring charts on the user's screen: activity over time, guardrail triggers, SLA-breach radar with countdowns, and LLM cost meter.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "show_report",
        "description": "Generate and display a report on the user's screen. Use kind 'handover' for a shift-handover briefing of the incident queue, or 'clusters' to group related open tickets by probable root cause.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["handover", "clusters"]},
            },
            "required": ["kind"],
        },
    },
    {
        "type": "function",
        "name": "hide_panels",
        "description": "Close all open panels on the user's screen.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def realtime_tools() -> list:
    """Chat-completions tool schemas flattened into Realtime format, plus the
    browser-executed screen-control tools."""
    return [
        {
            "type": "function",
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"],
        }
        for t in TOOLS
    ] + UI_TOOLS


@router.post("/api/voice/session")
async def mint_session():
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "OPENAI_API_KEY not configured"}, status_code=503)

    from app.prompts.voice_system import VOICE_SYSTEM_PROMPT

    body = {
        "expires_after": {"anchor": "created_at", "seconds": 600},
        "session": {
            "type": "realtime",
            "model": VOICE_MODEL,
            "instructions": VOICE_SYSTEM_PROMPT,
            "tools": realtime_tools(),
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "turn_detection": {"type": "semantic_vad"},
                    # whisper-1 was deprecated 2026-08-26; gpt-live-transcribe
                    # is the streaming replacement (verified accepted here).
                    "transcription": {"model": VOICE_TRANSCRIPTION_MODEL},
                },
                "output": {"voice": VOICE_NAME},
            },
        },
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                CLIENT_SECRETS_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json=body,
            )
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"OpenAI unreachable: {e}"}, status_code=502)

    if resp.status_code >= 400:
        # Surface OpenAI's error body verbatim — Realtime schemas move fast
        logger.warning("client_secrets error %s: %s", resp.status_code, resp.text[:500])
        return JSONResponse(
            {"error": "OpenAI rejected session", "detail": resp.text[:1000]},
            status_code=502,
        )

    data = resp.json()
    return {
        "value": data.get("value"),
        "expires_at": data.get("expires_at"),
        "model": VOICE_MODEL,
    }


@router.post("/api/voice/tool")
async def voice_tool(request: Request):
    from app.main import TOOL_HANDLERS  # late import — avoids circular import at startup

    body = await request.json()
    name = body.get("name", "")
    session_id = body.get("session_id", "voice")
    try:
        args = body.get("arguments")
        if isinstance(args, str):
            args = json.loads(args or "{}")
        args = args or {}
    except json.JSONDecodeError:
        args = {}

    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"output": json.dumps({"error": f"Unknown tool: {name}"})}

    # The text guardrails are bypassed on this channel — the human gate is not:
    # it sits in front of the handler, whoever calls it. The request simply
    # stays open until somebody clicks in the confirmation window.
    from app.approvals import approval_trigger, run_tool

    try:
        result, approval = await asyncio.to_thread(
            run_tool, name, args, handler, session_id, "voice-realtime"
        )
    except Exception as e:
        result, approval = json.dumps({"error": f"{name} failed: {e}"}), None

    triggers = ["guardrails_bypassed: realtime channel"]
    if approval:
        triggers.append(approval_trigger(approval))
    await asyncio.to_thread(
        log_interaction,
        user_input=f"[voice tool call] {name}({json.dumps(args, ensure_ascii=False)})",
        sanitized_input="",
        retrieved_chunks=[],
        model_response=str(result)[:500],
        tool_calls=[{"name": name, "arguments": args, "result": result}],
        judge_verdict={"verdict": "BYPASSED", "reason": "realtime voice channel"},
        guardrail_triggers=triggers,
    )

    if name == "create_incident":
        try:
            await hub.broadcast("incident_created", json.loads(result))
        except (json.JSONDecodeError, TypeError):
            pass

    return {"output": result}


@router.post("/api/voice/log")
async def voice_log(request: Request):
    body = await request.json()
    role = body.get("role", "user")
    transcript = (body.get("transcript") or "").strip()
    if not transcript:
        return {"status": "ok"}
    await asyncio.to_thread(
        log_interaction,
        user_input=transcript if role == "user" else "",
        sanitized_input="",
        retrieved_chunks=[],
        model_response=transcript if role == "assistant" else "",
        tool_calls=[],
        judge_verdict={"verdict": "BYPASSED", "reason": "realtime voice channel"},
        guardrail_triggers=["guardrails_bypassed: realtime channel"],
    )
    return {"status": "ok"}
