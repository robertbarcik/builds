"""Sentinel — a cheap LLM that watches the ops event stream and chirps in.

Runs alongside the simulation. Every cadence tick it reviews the recent
event window; when it spots a pattern it seeds a conversation session and
broadcasts a sentinel_message that the UI materializes as a new thread.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.config import SENTINEL_CADENCE_S
from app.conversations import seed_sentinel_session
from app.db import get_conn
from app.llm_client import chat_kwargs, get_role_client, parse_json_loosely
from app.ops.metrics import record_llm_usage
from app.prompts.sentinel_system import SENTINEL_SYSTEM_PROMPT
from app.tracing import tracer
from app.ws_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

MIN_NEW_EVENTS = 5
COOLDOWN_S = 180
WINDOW_S = 600
MAX_EVENTS_IN_PROMPT = 40


def _recent_window(window_s: int):
    start = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).isoformat()
    conn = get_conn()
    try:
        events = [
            {"id": r[0], "t": r[1][11:19], "type": r[2], "severity": r[3], "msg": r[4]}
            for r in conn.execute(
                "SELECT id, ts, event_type, severity, message FROM events "
                "WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (start, MAX_EVENTS_IN_PROMPT),
            )
        ]
        tickets = [
            {"id": r[0], "summary": r[1], "priority": r[2]}
            for r in conn.execute(
                "SELECT ticket_id, summary, priority FROM incidents "
                "WHERE created_at >= ? AND status IN ('open','escalated')",
                (start,),
            )
        ]
        total_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ?", (start,)
        ).fetchone()[0]
    finally:
        conn.close()
    return events, tickets, total_events


def _review_sync(events: list, tickets: list) -> dict:
    """One sentinel LLM review. Sync — runs in a worker thread."""
    lines = [json.dumps(e, ensure_ascii=False) for e in reversed(events)]
    if tickets:
        lines.append("OPEN TICKETS: " + json.dumps(tickets, ensure_ascii=False))
    user_msg = "\n".join(lines) or "No events."

    client, model = get_role_client("sentinel")
    try:
        with tracer.start_as_current_span(
            "gen_ai.chat",
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat",
                "mu.llm_call_type": "sentinel",
            },
        ) as span:
            t0 = time.monotonic()
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SENTINEL_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                # Generous budget: thinking models (qwen3, gpt-5.4) spend
                # tokens on reasoning before emitting the JSON verdict
                **chat_kwargs(model, max_tokens=900, temperature=0.1),
            )
            duration = time.monotonic() - t0
            span.set_attribute("mu.llm_duration_s", round(duration, 3))
            if resp.usage:
                record_llm_usage(
                    "sentinel", model,
                    resp.usage.prompt_tokens or 0,
                    resp.usage.completion_tokens or 0,
                    duration,
                )
            verdict = parse_json_loosely(resp.choices[0].message.content or "")
            span.set_attribute("mu.sentinel_alert", bool(verdict.get("alert")))
            return verdict
    except Exception as e:
        logger.warning("Sentinel review failed: %s", e)
        return {"alert": False}


class SentinelLoop:
    def __init__(self):
        self.running = False
        self._task = None
        self._reviewed_count = 0
        self._cooldowns = {}      # fingerprint -> monotonic ts
        self._last_alert_ts = 0.0

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Sentinel started")

    async def stop(self):
        if not self.running:
            return
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Sentinel stopped")

    def _on_cooldown(self, verdict: dict) -> bool:
        fingerprint = (verdict.get("headline") or "").lower().strip()[:40]
        now = time.monotonic()
        if now - self._last_alert_ts < 60:
            return True
        last = self._cooldowns.get(fingerprint, 0)
        if now - last < COOLDOWN_S:
            return True
        self._cooldowns[fingerprint] = now
        self._last_alert_ts = now
        return False

    async def review_once(self, force: bool = False) -> dict:
        events, tickets, total = await asyncio.to_thread(_recent_window, WINDOW_S)
        if not force and total - self._reviewed_count < MIN_NEW_EVENTS:
            return {"alert": False, "skipped": "not enough new events"}
        self._reviewed_count = total
        verdict = await asyncio.to_thread(_review_sync, events, tickets)
        if verdict.get("alert") and not self._on_cooldown(verdict):
            session_id = f"sentinel-{int(time.time())}"
            seed_sentinel_session(session_id, verdict)
            await hub.broadcast("sentinel_message", {
                "session_id": session_id,
                "severity": verdict.get("severity", "warning"),
                "headline": verdict.get("headline", "Sentinel alert"),
                "text": (verdict.get("finding", "") + "\n\n"
                         + verdict.get("suggested_action", "")).strip(),
                "related": verdict.get("related", []),
            })
        return verdict

    async def _loop(self):
        try:
            while self.running:
                await asyncio.sleep(SENTINEL_CADENCE_S)
                try:
                    await self.review_once()
                except Exception as e:
                    logger.warning("Sentinel tick failed: %s", e)
        except asyncio.CancelledError:
            pass


sentinel = SentinelLoop()


@router.post("/api/sentinel/review")
async def force_review():
    """Demo driver — force an immediate review regardless of gates."""
    return await sentinel.review_once(force=True)
