"""In-memory conversation store shared by the chat pipeline and the sentinel."""

from app.prompts.agent_system import AGENT_SYSTEM_PROMPT

# session_id -> messages
_conversations: dict = {}


def get_or_create(session_id: str) -> list:
    if session_id not in _conversations:
        _conversations[session_id] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    return _conversations[session_id]


def seed_sentinel_session(session_id: str, verdict: dict) -> None:
    """Pre-populate a session so a sentinel alert becomes a normal conversation.

    The alert is seeded as the first assistant message; when the user replies,
    the regular chat pipeline (guardrails included) takes over unchanged.
    """
    related = ", ".join(str(r) for r in verdict.get("related", [])) or "none listed"
    _conversations[session_id] = [
        {
            "role": "system",
            "content": AGENT_SYSTEM_PROMPT
            + "\n\n[SENTINEL CONTEXT] A monitoring sentinel raised this alert: "
            + verdict.get("finding", "")
            + f" Related items: {related}."
            + " If the user agrees to act, use your tools"
            + " (create_incident / escalate_ticket) to do it.",
        },
        {
            "role": "assistant",
            "content": verdict.get("headline", "Sentinel alert")
            + " — " + verdict.get("finding", "")
            + "\n\n" + verdict.get("suggested_action", ""),
        },
    ]
