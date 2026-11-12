"""Shift-handover briefing — the writer role reads the queue and reports."""

import json
import logging
import time
from datetime import datetime, timezone

from app.db import get_conn
from app.llm_client import chat_kwargs, get_role_client
from app.ops.metrics import guardrail_stats, record_llm_usage, sla_radar
from app.prompts.handover_system import HANDOVER_SYSTEM_PROMPT
from app.tracing import tracer

logger = logging.getLogger(__name__)


def _queue_snapshot() -> dict:
    conn = get_conn()
    try:
        open_tickets = [
            dict(zip(("ticket_id", "summary", "priority", "category", "status",
                      "reporter", "created_at", "escalation_reason"), r))
            for r in conn.execute(
                "SELECT ticket_id, summary, priority, category, status, reporter_name, "
                "created_at, escalation_reason FROM incidents "
                "WHERE status IN ('open', 'escalated') ORDER BY created_at ASC LIMIT 40"
            )
        ]
        resolved_today = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE status = 'resolved' "
            "AND created_at >= date('now')"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"open_tickets": open_tickets, "resolved_today": resolved_today}


def generate_handover() -> dict:
    snapshot = _queue_snapshot()
    radar = sla_radar()
    at_risk = [
        {"ticket_id": t["ticket_id"], "seconds_remaining": t["seconds_remaining"],
         "breached": t["breached"]}
        for t in radar["tickets"] if t["breached"] or t["seconds_remaining"] < 4 * 3600
    ]
    guardrails = guardrail_stats(hours=12)

    user_msg = json.dumps({
        "incident_queue": snapshot["open_tickets"],
        "resolved_today": snapshot["resolved_today"],
        "sla_at_risk": at_risk,
        "guardrail_activity_12h": guardrails["counts"],
    }, ensure_ascii=False)

    client, model = get_role_client("writer")
    with tracer.start_as_current_span(
        "gen_ai.chat",
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
            "mu.llm_call_type": "handover_writer",
        },
    ) as span:
        t0 = time.monotonic()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": HANDOVER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            **chat_kwargs(model, max_tokens=1200, temperature=0.3),
        )
        duration = time.monotonic() - t0
        span.set_attribute("mu.llm_duration_s", round(duration, 3))
        if resp.usage:
            record_llm_usage(
                "writer", model,
                resp.usage.prompt_tokens or 0,
                resp.usage.completion_tokens or 0,
                duration,
            )

    return {
        "markdown": resp.choices[0].message.content or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
    }
