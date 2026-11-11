"""Voice channel #2 — gpt-live-1 (full duplex) with CLIENT delegation.

How it fits together (see README "Voice: two generations"):

  browser ──WebRTC audio + data channel──▶ OpenAI Live session (gpt-live-1)
     │                                              ▲
     │ POST /api/voice/live/session {sdp}           │ sideband WebSocket
     ▼                                              │ (this module, trusted)
  FastAPI ── creates the session with the browser's SDP offer,
             then ATTACHES to it and listens for `session.delegation.created`.

  On a delegation the model keeps talking to the caller while we:
    1. reconstruct the request from the accumulated user transcript,
    2. run it through the NORMAL chat pipeline (input filters → agent + tools
       → output validation → LLM judge → audit log)  — same code as a thread,
    3. narrate progress to the model with `session.thinking.append`
       (silent context: "tool check_sla finished …"),
    4. hand the answer back with `session.commentary.append` (spoken), or a
       `session.instructions.append` "stop" when the guardrails refused.

  Every stage is also pushed to the UI over /ws (`voice_delegation`) so the
  operator can watch the backend work while the call continues.

The older gpt-realtime path (app/voice.py) stays available as a fallback and
as the "no guardrails" contrast. Which one the orb uses is a runtime switch.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import (
    AUDIT_LOG_PATH,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    VOICE_LIVE_MODEL,
    VOICE_MODE_DEFAULT,
    VOICE_NAME,
    VOICE_REALTIME_MODEL,
)

VOICE_LOG_DIR = os.path.join(os.path.dirname(AUDIT_LOG_PATH), "voice")

# gpt-live-transcribe annotates non-speech sounds like "[inhale]" or
# "[tongue click ]"; they are noise for the backend and for the captions.
_NONSPEECH_RE = re.compile(r"\[[^\]]{0,40}\]")


def clean_delta(text: str) -> str:
    return _NONSPEECH_RE.sub("", text or "")
from app.guardrails.audit import log_interaction
from app.ops.metrics import LIVE_VOICE_USD_PER_MIN, record_llm_usage
from app.prompts.voice_live_system import VOICE_LIVE_INSTRUCTIONS
from app.ws_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Voice mode switch (live | realtime) ─────────────────────────────

_state = {"mode": VOICE_MODE_DEFAULT if VOICE_MODE_DEFAULT in ("live", "realtime") else "live"}


def voice_mode_state() -> dict:
    mode = _state["mode"]
    return {
        "mode": mode,
        "model": VOICE_LIVE_MODEL if mode == "live" else VOICE_REALTIME_MODEL,
        "guardrails": mode == "live",
    }


@router.get("/api/voice/mode")
async def get_voice_mode():
    return voice_mode_state()


@router.post("/api/voice/mode")
async def set_voice_mode(request: Request):
    body = await request.json()
    mode = body.get("mode")
    if mode not in ("live", "realtime"):
        return JSONResponse({"error": "mode must be 'live' or 'realtime'"}, status_code=400)
    _state["mode"] = mode
    state = voice_mode_state()
    await hub.broadcast("voice_mode", state)
    return state


# ── Session config ──────────────────────────────────────────────────

# What the (untrusted) browser data channel may do. It only LISTENS: the
# browser needs transcripts and delegation markers to draw the conversation,
# but must not be able to append instructions or context — that is the
# trusted sideband's job. Nice teaching point: the security boundary is the
# session config, not the frontend code.
DATA_CHANNEL_POLICY = {
    "allowed_client_events": [],
    "allowed_server_events": [
        {"type": t}
        for t in (
            "session.started",
            "session.input_transcript.delta",
            "session.output_transcript.delta",
            "session.delegation.created",
            "session.usage.updated",
            "session.closed",
            "info",
            "error",
        )
    ],
}


def live_session_config(with_client_policy: bool = True) -> dict:
    cfg = {
        "model": VOICE_LIVE_MODEL,
        "instructions": VOICE_LIVE_INSTRUCTIONS,
        "audio": {"output": {"voice": VOICE_NAME}},
        "delegation": {"type": "client"},
    }
    if with_client_policy:
        cfg["client"] = {"data_channel": DATA_CHANNEL_POLICY}
    return cfg


def _openai_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


# ── The bridge: one per live session ────────────────────────────────


class LiveBridge:
    """Listens on a Live session (sideband or primary WS) and services client
    delegations through the guardrail pipeline. Transport-agnostic: `conn`
    only needs `.send(dict)` and async iteration of parsed server events."""

    def __init__(self, session_id: str, ui_session_id: str = ""):
        self.session_id = session_id
        self.ui_session_id = ui_session_id or session_id
        self.started_at = time.time()
        # Per-call debug log: logs/voice/<utc stamp>-<session>.jsonl — every
        # transcript turn, delegation (with the full pipeline result), append
        # we sent, error and the close. This is what to read when a call
        # "felt wrong": the audit log only has the guardrail view.
        os.makedirs(VOICE_LOG_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.log_path = os.path.join(VOICE_LOG_DIR, f"{stamp}-{session_id[-8:]}.jsonl")
        self._open_turn = None   # (role, text, start_ms, end_ms) being accumulated
        self.user_buf: list = []        # (start_ms, text) user fragments not yet delegated
        self.last_assistant_end_ms = 0  # turn boundary: user speech after this is "the current request"
        self.transcript: list = []      # (role, text) — whole call, for the audit log
        self._closing = False
        self.delegations = 0
        self.usage_seconds = 0.0
        self.close_reason: Optional[str] = None
        self._conn = None
        self._task: Optional[asyncio.Task] = None
        self._pending: set = set()
        self._awaiting_human: Optional[str] = None   # summary of the action waiting for a click

    # -- lifecycle ----------------------------------------------------

    async def run_sideband(self):
        client = _openai_client()
        try:
            async with client.live.sideband.connect(session_id=self.session_id) as conn:
                self._conn = conn
                async for event in conn:
                    await self.handle_event(conn, event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # When the browser hangs up, OpenAI tears the sideband down without
            # a close frame — that's a normal end of call, not an error.
            if self._closing or "close frame" in str(e).lower() or type(e).__name__.startswith("ConnectionClosed"):
                logger.info("live sideband %s closed (%s)", self.session_id, type(e).__name__)
                if self.close_reason is None:
                    self.close_reason = "hangup"
            else:
                logger.warning("live sideband %s ended: %s", self.session_id, e)
                await hub.broadcast("voice_error", {"session_id": self.session_id, "error": str(e)[:300]})
        finally:
            await self._finalize()

    async def close(self):
        self._closing = True
        if self._conn is not None:
            try:
                await self._conn.send({"type": "session.close"})
            except Exception:
                pass

    async def _finalize(self):
        if self.close_reason is None:
            self.close_reason = "connection_lost"
        minutes = self.usage_seconds / 60.0
        cost = minutes * LIVE_VOICE_USD_PER_MIN
        self._flush_turn()
        self._vlog("session_closed", reason=self.close_reason, seconds=self.usage_seconds,
                   cost_usd=round(cost, 4), delegations=self.delegations)
        try:
            await asyncio.to_thread(
                record_llm_usage, "voice", VOICE_LIVE_MODEL, 0, 0,
                self.usage_seconds, session_id=self.ui_session_id, cost_override=cost,
            )
        except Exception:
            logger.debug("voice usage record failed", exc_info=True)
        # One audit record per call: the transcript. Individual delegations
        # were already logged (with real judge verdicts) by the pipeline.
        convo = "\n".join(f"{r}: {t}" for r, t in self.transcript if t.strip())
        if convo:
            await asyncio.to_thread(
                log_interaction,
                user_input=f"[voice call transcript · {self.delegations} delegations · {self.usage_seconds:.0f}s]",
                sanitized_input="",
                retrieved_chunks=[],
                model_response=convo[-4000:],
                tool_calls=[],
                judge_verdict={"verdict": "N/A", "reason": "call transcript; each delegation judged separately"},
                guardrail_triggers=[f"channel_voice_live: transcript ({self.close_reason})"],
            )
        await hub.broadcast("voice_closed", {
            "session_id": self.session_id,
            "reason": self.close_reason,
            "seconds": round(self.usage_seconds, 1),
            "cost_usd": round(cost, 4),
            "delegations": self.delegations,
        })
        _bridges.pop(self.session_id, None)

    # -- events -------------------------------------------------------

    async def handle_event(self, conn, ev):
        t = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
        get = (lambda k, d=None: getattr(ev, k, d)) if not isinstance(ev, dict) else (lambda k, d=None: ev.get(k, d))

        if t == "session.input_transcript.delta":
            delta = clean_delta(get("delta", ""))
            self.user_buf.append((int(get("start_ms", 0) or 0), delta))
            self._append_transcript("user", delta, get("start_ms"), get("end_ms"))
        elif t == "session.output_transcript.delta":
            self.last_assistant_end_ms = max(self.last_assistant_end_ms, int(get("end_ms", 0) or 0))
            self._append_transcript("assistant", get("delta", ""), get("start_ms"), get("end_ms"))
        elif t == "session.started":
            self._vlog("session_started", model=VOICE_LIVE_MODEL)
        elif t == "session.delegation.created":
            d = get("delegation")
            target = getattr(d, "target", None) if d is not None and not isinstance(d, dict) else (d or {}).get("target")
            did = getattr(d, "id", None) if d is not None and not isinstance(d, dict) else (d or {}).get("id")
            if target == "client" and did:
                task = asyncio.create_task(self._delegate(conn, did, get("offset_ms", 0)))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        elif t == "session.usage.updated":
            u = get("usage")
            self.usage_seconds = float(getattr(u, "seconds", 0) if u is not None and not isinstance(u, dict) else (u or {}).get("seconds", 0))
        elif t == "session.closed":
            u = get("usage")
            if u is not None:
                self.usage_seconds = float(getattr(u, "seconds", 0) if not isinstance(u, dict) else u.get("seconds", 0))
            self.close_reason = get("reason", "close_requested")
        elif t == "error":
            err = get("error")
            msg = getattr(err, "message", None) if err is not None and not isinstance(err, dict) else (err or {}).get("message")
            code = getattr(err, "code", None) if err is not None and not isinstance(err, dict) else (err or {}).get("code")
            logger.warning("live session %s error: %s", self.session_id, msg)
            self._vlog("error", code=code, message=str(msg)[:300])
            await hub.broadcast("voice_error", {"session_id": self.session_id, "error": str(msg)[:300]})

    # -- per-call log ---------------------------------------------------

    def _vlog(self, kind: str, **data):
        rec = {"t": round(time.time() - self.started_at, 2), "kind": kind, **data}
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("voice log write failed", exc_info=True)

    def _append_transcript(self, role: str, delta: str, start_ms=None, end_ms=None):
        if self.transcript and self.transcript[-1][0] == role:
            self.transcript[-1] = (role, self.transcript[-1][1] + delta)
        else:
            self.transcript.append((role, delta))
        # Log whole turns, not deltas: flush the previous speaker's turn when
        # the other one starts.
        if self._open_turn and self._open_turn[0] != role:
            self._flush_turn()
        if self._open_turn is None:
            self._open_turn = [role, "", start_ms, end_ms]
        self._open_turn[1] += delta
        if end_ms is not None:
            self._open_turn[3] = end_ms

    def _flush_turn(self):
        if self._open_turn and self._open_turn[1].strip():
            role, text, s, e = self._open_turn
            self._vlog("turn", role=role, text=text.strip(), start_ms=s, end_ms=e)
        self._open_turn = None

    def _request_text(self) -> str:
        # The current request = what the caller said since the assistant last
        # spoke (a greeting turn answered without delegation must not leak into
        # the next request). Small slack: the model may start speaking ("let
        # me check…") a beat before the delegation event lands.
        cutoff = self.last_assistant_end_ms - 1500
        current = [txt for start, txt in self.user_buf if start >= cutoff]
        text = "".join(current).strip()
        self.user_buf = []
        if len(text) < 8:
            # Delegation fired on a short fragment (e.g. "yes, do it") — give the
            # backend the recent exchange so the agent has the referent.
            recent = [f"{r}: {t.strip()}" for r, t in self.transcript[-4:] if t.strip()]
            text = ("Recent conversation:\n" + "\n".join(recent) + "\n\nCaller's latest request: " + text) if recent else text
        return text or "(no transcript available yet)"

    # -- the delegation itself ---------------------------------------

    async def _delegate(self, conn, delegation_id: str, offset_ms: int):
        from app.main import _run_chat_pipeline  # late import: main imports this module

        self.delegations += 1
        seq = self.delegations
        request = self._request_text()
        t0 = time.monotonic()
        base = {"session_id": self.session_id, "delegation_id": delegation_id, "seq": seq}
        self._flush_turn()
        self._vlog("delegation_created", seq=seq, delegation_id=delegation_id, offset_ms=offset_ms, request=request)

        # A confirmation window is open and the pipeline of the earlier
        # delegation is parked on it. Whatever the caller just said ("yes, go
        # ahead", "I confirm") must not start a second run — and must not
        # count as the confirmation either: only the click does.
        if self._awaiting_human:
            self._vlog("delegation_parked", seq=seq, awaiting=self._awaiting_human)
            await conn.send({
                "type": "session.commentary.append",
                "event_id": f"d{seq}-parked",
                "delegation_id": delegation_id,
                "content": (
                    f"I'm still waiting for the click in the confirmation window for: {self._awaiting_human}. "
                    "Saying yes isn't enough here — please click Approve or Deny on the screen."
                ),
            })
            await hub.broadcast("voice_delegation", {**base, "stage": "parked", "awaiting": self._awaiting_human})
            return

        await hub.broadcast("voice_delegation", {**base, "stage": "created", "request": request[:400], "offset_ms": offset_ms})

        await conn.send({
            "type": "session.thinking.append",
            "event_id": f"d{seq}-ack",
            "delegation_id": delegation_id,
            "content": (
                f"Backend received the request: \"{request[:300]}\". Security guardrails "
                "and tools are running now. If the caller asks, say it is being checked. "
                "Do not guess numbers, ticket ids or outcomes."
            ),
        })

        loop = asyncio.get_running_loop()

        def progress(stage: str, detail: dict):
            # Called from the pipeline's worker thread. tool_done carries the
            # parsed result as `data` — the UI renders it as a card while the
            # model is still talking.
            hub.broadcast_threadsafe("voice_delegation", {**base, "stage": stage, **detail})

            def say(kind: str, tag: str, content: str):
                self._vlog("append", seq=seq, type=kind, tag=tag, content=content)
                asyncio.run_coroutine_threadsafe(conn.send({
                    "type": f"session.{kind}.append",
                    "event_id": f"d{seq}-{tag}",
                    "delegation_id": delegation_id,
                    "content": content,
                }), loop)

            # Human oversight: the pipeline is parked inside the tool call
            # until somebody clicks. The voice must say so NOW (commentary =
            # spoken), not when the delegation finishes.
            if stage == "approval_required":
                self._awaiting_human = detail.get("summary") or "the requested action"
                say("commentary", f"approval-open-{detail.get('approval_id')}",
                    f"I can do that: {self._awaiting_human}. Because it is a high-impact action, a "
                    "confirmation window has just popped up on your screen. Please click Approve there "
                    "— or Deny — and I'll wait for you.")
                return
            if stage == "approval_resolved":
                self._awaiting_human = None
                decision = detail.get("decision")
                tag = f"approval-{decision}-{detail.get('approval_id')}"
                if decision == "approved":
                    say("commentary", tag, f"Thanks for the confirmation. Going ahead with it now: {detail.get('summary')}.")
                elif decision == "denied":
                    say("thinking", tag, f"The caller clicked DENY for: {detail.get('summary')}. Nothing was changed. "
                                         "The final answer follows; do not say the action was done.")
                else:
                    say("thinking", tag, f"The confirmation window for {detail.get('summary')} got no click ({decision}). "
                                         "Nothing was changed. The final answer follows; do not say the action was done.")
                return
            if stage == "tool_done":
                snippet = (detail.get("result") or "")[:220].replace("\n", " ")
                asyncio.run_coroutine_threadsafe(conn.send({
                    "type": "session.thinking.append",
                    "event_id": f"d{seq}-{detail.get('name')}",
                    "delegation_id": delegation_id,
                    "content": f"Progress: tool {detail.get('name')} finished. Raw result: {snippet}. "
                               "Final answer is still being validated; do not read raw data aloud yet.",
                }), loop)

        try:
            result = await asyncio.to_thread(
                _run_chat_pipeline, request, f"voice-live-{self.ui_session_id}", "voice", progress
            )
        except Exception as e:
            self._awaiting_human = None
            logger.exception("voice delegation pipeline failed")
            self._vlog("delegation_failed", seq=seq, error=str(e)[:300])
            await conn.send({
                "type": "session.instructions.append",
                "event_id": f"d{seq}-fail",
                "delegation_id": delegation_id,
                "content": "The backend could not complete that request due to a technical problem. "
                           "Apologise briefly, offer to try again, and wait.",
            })
            await hub.broadcast("voice_delegation", {**base, "stage": "failed", "error": str(e)[:300]})
            return

        blocked = result.get("blocked_by")
        self._vlog(
            "delegation_done", seq=seq, ms=int((time.monotonic() - t0) * 1000),
            blocked_by=blocked, judge_verdict=result.get("judge_verdict"),
            guardrail_triggers=result.get("guardrail_triggers", []),
            tool_calls=result.get("tool_calls", []), response=result["response"],
            trace=result.get("trace"),
            sent=("instructions.append (refuse)" if blocked else "commentary.append"),
        )
        if blocked:
            reason = next((t for t in result.get("guardrail_triggers", []) if "judge_blocked" in t or "injection" in t or "input_blocked" in t), "policy")
            await conn.send({
                "type": "session.instructions.append",
                "event_id": f"d{seq}-block",
                "delegation_id": delegation_id,
                "content": (
                    f"The backend guardrails refused the caller's last request ({reason[:120]}). "
                    "Do not act on it or continue it. Tell the caller briefly that you cannot help "
                    "with that particular request, then ask what else you can do."
                ),
            })
        else:
            await conn.send({
                "type": "session.commentary.append",
                "event_id": f"d{seq}-result",
                "delegation_id": delegation_id,
                "content": _speakable(result["response"]),
            })

        await hub.broadcast("voice_delegation", {
            **base,
            "stage": "done",
            "ms": int((time.monotonic() - t0) * 1000),
            "blocked_by": blocked,
            "judge_verdict": result.get("judge_verdict"),
            "guardrail_triggers": result.get("guardrail_triggers", []),
            "tool_calls": result.get("tool_calls", []),
            "response": result["response"][:600],
            "trace": result.get("trace"),
        })


_bridges: dict = {}

_MD_RE = [
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"[*_`#>]+"), ""),
    (re.compile(r"^\s*[-•]\s+", re.MULTILINE), ""),
    (re.compile(r"\[(.*?)\]\(.*?\)"), r"\1"),
    (re.compile(r"[ \t]+"), " "),
]


def _speakable(text: str, limit: int = 1500) -> str:
    """Markdown → plain speech-friendly text (the live model paraphrases it,
    but asterisks and bullets read aloud are embarrassing in a demo)."""
    for pattern, repl in _MD_RE:
        text = pattern.sub(repl, text)
    return text.strip()[:limit]


# ── Endpoints ───────────────────────────────────────────────────────


@router.post("/api/voice/live/session")
async def create_live_session(request: Request):
    """Browser sends its WebRTC SDP offer; we create the Live session server-side
    (the API key never reaches the browser) and attach the sideband bridge."""
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "OPENAI_API_KEY not configured"}, status_code=503)
    body = await request.json()
    sdp = body.get("sdp")
    ui_session_id = body.get("session_id") or f"voice-{int(time.time())}"
    if not sdp:
        return JSONResponse({"error": "missing sdp offer"}, status_code=400)

    from openai import BadRequestError

    client = _openai_client()
    try:
        try:
            created = await client.live.create(
                session=live_session_config(with_client_policy=True),
                transport={"type": "webrtc", "sdp": sdp},
            )
        except BadRequestError as e:
            # Schema drift on the frontend-permissions block shouldn't kill the
            # demo — retry once without it (the browser then simply sees more).
            if "client" in str(e).lower() or "data_channel" in str(e).lower():
                logger.warning("live.create rejected client policy, retrying without: %s", str(e)[:200])
                created = await client.live.create(
                    session=live_session_config(with_client_policy=False),
                    transport={"type": "webrtc", "sdp": sdp},
                )
            else:
                raise
    except Exception as e:
        logger.warning("live.create failed: %s", str(e)[:500])
        return JSONResponse({"error": "OpenAI rejected live session", "detail": str(e)[:1000]}, status_code=502)

    session_id = created.session.id
    bridge = LiveBridge(session_id, ui_session_id)
    _bridges[session_id] = bridge
    bridge._task = asyncio.create_task(bridge.run_sideband())
    logger.info("live session %s created (ui %s)", session_id, ui_session_id)
    await hub.broadcast("voice_live_started", {"session_id": session_id, "model": VOICE_LIVE_MODEL})
    return {
        "session_id": session_id,
        "sdp": created.transport.sdp,
        "model": VOICE_LIVE_MODEL,
        "guardrails": True,
    }


@router.post("/api/voice/live/end")
async def end_live_session(request: Request):
    body = await request.json()
    bridge = _bridges.get(body.get("session_id", ""))
    if bridge:
        await bridge.close()
    return {"status": "ok"}


@router.get("/api/voice/live/sessions")
async def list_live_sessions():
    return {
        "sessions": [
            {
                "session_id": b.session_id,
                "delegations": b.delegations,
                "seconds": round(b.usage_seconds, 1),
                "age_s": int(time.time() - b.started_at),
            }
            for b in _bridges.values()
        ]
    }
