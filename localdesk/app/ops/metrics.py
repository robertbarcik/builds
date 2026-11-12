"""Request-level LLM cost accounting + monitoring query endpoints."""

import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.config import AUDIT_LOG_PATH
from app.db import get_conn
from app.tools.sla import SLA_DATA
from app.ws_hub import hub

router = APIRouter()

MODEL_COSTS = {  # USD per 1M tokens (input, output) — update at demo time
    "qwen3:1.7b":            (0.0,  0.0),
    "qwen3.5:4b":            (0.0,  0.0),
    "qwen/qwen3-30b-a3b":    (0.12, 0.50),
    "gpt-5.4-nano":          (0.20, 1.25),
    "gpt-5.4-mini":          (0.75, 4.50),
    "gpt-6-luna":            (0.10, 0.50),   # prices as of 2026-09-22 cut
    "gpt-6.1-sol":           (2.00, 10.00),
    "gpt-realtime-2.1-mini": (0.60, 2.40),  # text-token proxy; voice cost shown as approx
}
# gpt-live-1 is billed by duration, not tokens: $0.05 per minute, per second.
LIVE_VOICE_USD_PER_MIN = 0.05


def record_llm_usage(
    role: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_s: float,
    session_id: str = "",
    cost_override: float | None = None,
) -> float:
    """Insert one request_metrics row and push a live cost update.

    Called from worker threads (sync pipeline) — uses broadcast_threadsafe.
    cost_override: for duration-billed models (gpt-live-1) the caller computes
    the cost itself instead of the per-token table.
    """
    inp, out = MODEL_COSTS.get(model, (0.0, 0.0))
    cost = prompt_tokens / 1e6 * inp + completion_tokens / 1e6 * out
    if cost_override is not None:
        cost = cost_override
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO request_metrics "
            "(ts, session_id, role, model, prompt_tokens, completion_tokens, cost_usd, duration_s) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                session_id, role, model,
                prompt_tokens, completion_tokens,
                cost, round(duration_s, 3),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    hub.broadcast_threadsafe("chart_update", {
        "kind": "cost",
        "role": role,
        "model": model,
        "tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(cost, 6),
    })
    return cost


# ── SLA deadline helpers ────────────────────────────────────────────

_DUR_RE = re.compile(r"(\d+)\s*(business\s+day|minute|hour|day)", re.IGNORECASE)
_UNIT_S = {"minute": 60, "hour": 3600, "day": 86400, "business day": 86400}


def parse_duration_s(text: str) -> int:
    m = _DUR_RE.search(text)
    if not m:
        return 0
    unit = re.sub(r"\s+", " ", m.group(2).lower())
    return int(m.group(1)) * _UNIT_S.get(unit, 0)


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/api/metrics/timeline")
def timeline(minutes: int = 60, bucket: int = 5):
    """Incident + event counts per time bucket (for the incidents-over-time chart)."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    bucket_s = max(60, bucket * 60)
    conn = get_conn()
    try:
        def counts(table: str, ts_col: str) -> dict:
            rows = conn.execute(
                f"SELECT CAST(strftime('%s', {ts_col}) AS INTEGER) / {bucket_s} AS b, COUNT(*) "
                f"FROM {table} WHERE {ts_col} >= ? GROUP BY b",
                (start.isoformat(),),
            ).fetchall()
            return {r[0]: r[1] for r in rows}

        inc = counts("incidents", "created_at")
        ev = counts("events", "ts")
    finally:
        conn.close()

    first_b = int(start.timestamp()) // bucket_s
    last_b = int(now.timestamp()) // bucket_s
    buckets = [
        {"t": b * bucket_s, "incidents": inc.get(b, 0), "events": ev.get(b, 0)}
        for b in range(first_b, last_b + 1)
    ]
    return {"buckets": buckets, "bucket_minutes": bucket_s // 60}


@router.get("/api/metrics/guardrails")
def guardrail_stats(hours: int = 24):
    """Trigger counts parsed from the audit log."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    counts = {
        "injection": 0, "input_blocked": 0, "pii_redacted": 0, "pii_output": 0,
        "hallucinated_sla": 0, "judge_flag": 0, "judge_block": 0, "judge_unavailable": 0,
        "authorized_disclosure": 0, "bypassed_realtime": 0, "voice_live_guarded": 0,
        "human_approved": 0, "human_denied": 0,
    }
    total = 0
    try:
        with open(AUDIT_LOG_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp", "")
                try:
                    if datetime.fromisoformat(ts) < cutoff:
                        continue
                except ValueError:
                    continue
                total += 1
                for trig in rec.get("guardrail_triggers", []):
                    if "injection" in trig:
                        counts["injection"] += 1
                    elif "input_blocked" in trig:
                        counts["input_blocked"] += 1
                    elif "pii_redacted" in trig:
                        counts["pii_redacted"] += 1
                    elif "PII detected" in trig:
                        counts["pii_output"] += 1
                    elif "hallucinated SLA" in trig:
                        counts["hallucinated_sla"] += 1
                    elif "judge_flagged" in trig:
                        counts["judge_flag"] += 1
                    elif "judge_blocked" in trig:
                        counts["judge_block"] += 1
                    elif "judge_unavailable" in trig:
                        counts["judge_unavailable"] += 1
                    elif "note_human_approval" in trig:
                        key = "human_approved" if ": approved" in trig else "human_denied"
                        counts[key] += 1
                    elif "note_authorized_disclosure" in trig:
                        counts["authorized_disclosure"] += 1
                    elif "guardrails_bypassed" in trig:
                        counts["bypassed_realtime"] += 1
                    elif "channel_voice_live" in trig:
                        counts["voice_live_guarded"] += 1
    except FileNotFoundError:
        pass
    return {"total_interactions": total, "counts": counts, "hours": hours}


@router.get("/api/metrics/sla-radar")
def sla_radar():
    """Open tickets vs their SLA resolution deadlines."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT i.ticket_id, i.summary, i.priority, i.status, i.created_at, "
            "       COALESCE(e.customer_tier, 'silver') AS tier "
            "FROM incidents i LEFT JOIN employees e ON e.name = i.reporter_name "
            "WHERE i.status IN ('open', 'escalated') "
            "ORDER BY i.created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    tickets = []
    for ticket_id, summary, priority, status, created_at, tier in rows:
        tier_data = SLA_DATA.get(tier, SLA_DATA["silver"])
        prio_data = tier_data["priorities"].get(priority)
        if not prio_data:
            continue
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        deadline = created + timedelta(seconds=parse_duration_s(prio_data["resolution_time"]))
        remaining = int((deadline - now).total_seconds())
        tickets.append({
            "ticket_id": ticket_id,
            "summary": summary,
            "priority": priority,
            "tier": tier,
            "status": status,
            "created_at": created_at,
            "resolution_deadline": deadline.isoformat(),
            "seconds_remaining": remaining,
            "breached": remaining < 0,
        })
    tickets.sort(key=lambda t: t["seconds_remaining"])
    return {"tickets": tickets}


@router.get("/api/metrics/costs")
def costs(minutes: int = 60):
    start = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn = get_conn()
    try:
        by_role = {}
        for role, model, tokens, cost, n in conn.execute(
            "SELECT role, model, SUM(prompt_tokens + completion_tokens), "
            "       SUM(cost_usd), COUNT(*) "
            "FROM request_metrics WHERE ts >= ? GROUP BY role, model",
            (start,),
        ):
            entry = by_role.setdefault(role, {"tokens": 0, "cost_usd": 0.0, "calls": 0, "models": []})
            entry["tokens"] += tokens or 0
            entry["cost_usd"] += cost or 0.0
            entry["calls"] += n
            entry["models"].append(model)

        series = [
            {"t": r[0], "cost_usd": r[1] or 0.0}
            for r in conn.execute(
                "SELECT CAST(strftime('%s', ts) AS INTEGER) / 60 * 60, SUM(cost_usd) "
                "FROM request_metrics WHERE ts >= ? GROUP BY 1 ORDER BY 1",
                (start,),
            )
        ]
    finally:
        conn.close()

    for entry in by_role.values():
        entry["cost_usd"] = round(entry["cost_usd"], 6)
    return {
        "total_usd": round(sum(e["cost_usd"] for e in by_role.values()), 6),
        "total_tokens": sum(e["tokens"] for e in by_role.values()),
        "by_role": by_role,
        "series": series,
        "minutes": minutes,
    }
