"""audit_report tool — lets the desk agent (and therefore the voice) answer
"what did the guardrails block today?" without switching to the Ω audit chat.

Reuses the same readers as the audit meta-chat so both channels agree."""

import json


def audit_report(hours: int = 24, limit: int = 5) -> str:
    from app.audit_chat import list_flagged  # late import: audit_chat imports metrics
    from app.ops.metrics import guardrail_stats

    try:
        hours = max(1, min(int(hours), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    try:
        limit = max(1, min(int(limit), 15))
    except (TypeError, ValueError):
        limit = 5
    stats = guardrail_stats(hours=hours)
    flagged = json.loads(list_flagged(hours=hours, limit=limit)).get("flagged", [])
    counts = {k: v for k, v in stats["counts"].items() if v}
    return json.dumps({
        "hours": hours,
        "total_interactions": stats["total_interactions"],
        "trigger_counts": counts,
        "legend": {
            "injection": "prompt-injection attempts blocked at gate one",
            "input_blocked": "inputs rejected at gate one (length etc.)",
            "pii_redacted": "personal data redacted from user input",
            "pii_output": "personal data caught in a model answer",
            "hallucinated_sla": "SLA numbers with no source, flagged at gate two",
            "judge_flag": "answers the judge flagged",
            "judge_block": "answers the judge blocked and replaced",
            "judge_unavailable": "turns where the judge could not run",
            "authorized_disclosure": "personal data legitimately returned by a tool",
            "bypassed_realtime": "voice turns on the old realtime channel (no guardrails)",
            "voice_live_guarded": "voice turns that went through the full pipeline",
            "human_approved": "high-impact server actions a human approved in the confirmation window",
            "human_denied": "high-impact server actions a human denied or left unanswered (not executed)",
        },
        "recent_flagged": flagged,
    }, ensure_ascii=False)
