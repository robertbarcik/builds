"""Audit logger — writes structured JSONL for every interaction."""

import json
import os
from datetime import datetime, timezone

from app.config import AUDIT_LOG_PATH


def log_interaction(
    user_input: str,
    sanitized_input: str,
    retrieved_chunks: list[dict],
    model_response: str,
    tool_calls: list[dict],
    judge_verdict: dict,
    guardrail_triggers: list[str],
) -> None:
    """Append one audit record to the JSONL log."""
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_input_original": user_input,
        "user_input_sanitized": sanitized_input,
        "retrieved_chunks": [
            {"source": c.get("source", ""), "text": c.get("text", "")[:200]}
            for c in retrieved_chunks
        ],
        "model_response": model_response,
        "tool_calls": tool_calls,
        "judge_verdict": judge_verdict,
        "guardrail_triggers": guardrail_triggers,
    }
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
