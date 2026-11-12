"""Human oversight gate — high-impact tool calls wait for a human click.

The model may ASK for a reboot, a termination or a migration; it cannot
perform one. When the agent calls such a tool, `run_tool` below does not call
the handler. It registers a pending approval, pushes `approval_requested` to
every connected browser (a confirmation window pops up) and BLOCKS the
pipeline's worker thread until a human answers `POST /api/approvals/{id}`,
or the request times out. Only "approved" reaches the handler.

Design points worth showing in a lecture:

  * The tier is decided in code from the tool name and its ARGUMENTS
    (app/tools/servers.py APPROVAL_POLICY) — a small server is bought
    directly, a large one needs a human. No prompt can change that.
  * The approval travels on a channel the model cannot write to (an HTTP
    call from the operator's browser). A typed or spoken "yes, I confirm"
    goes to the model and therefore proves nothing.
  * It fails closed: denied, timed out, or nobody there to ask → the handler
    is never called and the model is told exactly that.
  * One gate for every channel: text threads, live voice (the delegation
    simply takes longer; the voice is told to announce the window and wait),
    the realtime voice bridge and MCP all go through `run_tool`.
"""

import json
import logging
import threading
import time
import uuid
from typing import Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.tools.servers import APPROVAL_POLICY
from app.ws_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

TIMEOUT_S = 120

_pending: dict = {}          # id -> request dict (with a threading.Event)
_lock = threading.Lock()


def approver_present() -> bool:
    """Is there a screen that can show the confirmation window? (Replaced in
    the headless smoke test.)"""
    return hub.client_count > 0


def _public(req: dict) -> dict:
    out = {k: v for k, v in req.items() if not k.startswith("_")}
    out["remaining_s"] = max(0, int(req["_deadline"] - time.time()))
    return out


def request_approval(tool: str, args: dict, card: dict, session_id: str = "",
                     channel: str = "text", on_open: Optional[Callable] = None) -> dict:
    """Ask a human; block until answered. Call from a worker thread only.

    Returns {"id", "decision": approved | denied | expired | unavailable, "wait_ms"}.
    """
    if not approver_present():
        return {"id": None, "decision": "unavailable", "wait_ms": 0}

    req = {
        "id": uuid.uuid4().hex[:10],
        "tool": tool,
        "arguments": args,
        "action": card.get("action", tool),
        "target": card.get("target", ""),
        "details": card.get("details", []),
        "impact": card.get("impact", ""),
        "channel": channel,
        "session_id": session_id,
        "timeout_s": TIMEOUT_S,
        "_deadline": time.time() + TIMEOUT_S,
        "_event": threading.Event(),
        "_decision": None,
    }
    with _lock:
        _pending[req["id"]] = req
    t0 = time.monotonic()
    logger.info("approval %s requested: %s %s", req["id"], req["action"], req["target"])
    hub.broadcast_threadsafe("approval_requested", _public(req))
    if on_open:
        try:
            on_open(_public(req))
        except Exception:
            logger.debug("approval on_open failed", exc_info=True)

    req["_event"].wait(TIMEOUT_S)

    with _lock:
        _pending.pop(req["id"], None)
        decision = req["_decision"] or "expired"
    wait_ms = int((time.monotonic() - t0) * 1000)
    logger.info("approval %s %s after %d ms", req["id"], decision, wait_ms)
    hub.broadcast_threadsafe("approval_resolved", {
        "id": req["id"], "decision": decision, "tool": tool,
        "action": req["action"], "target": req["target"], "wait_ms": wait_ms,
    })
    return {"id": req["id"], "decision": decision, "wait_ms": wait_ms}


def resolve(approval_id: str, decision: str) -> bool:
    """Record the human's answer. False if the request is gone (answered, expired)."""
    with _lock:
        req = _pending.get(approval_id)
        if not req or req["_decision"]:
            return False
        req["_decision"] = decision
    req["_event"].set()
    return True


_NOT_EXECUTED = {
    "denied": "A human operator DENIED this action in the confirmation window. Nothing was changed. "
              "Tell the user it was not carried out. Do not retry unless they ask again.",
    "expired": f"Nobody answered the confirmation window within {TIMEOUT_S} seconds, so the action was "
               "NOT carried out. Nothing was changed. The user can ask again.",
    "unavailable": "This action needs a human to confirm it in the operations screen, and no such screen "
                   "is connected on this channel. The action was NOT carried out.",
}


def run_tool(name: str, args: dict, handler: Callable, session_id: str = "",
             channel: str = "text", report: Optional[Callable] = None):
    """Run a tool handler, through the human gate when the risk policy says so.

    Returns (result_json: str, approval: dict | None). `report(stage, **detail)`
    is the pipeline's progress callback (approval_required / approval_resolved).
    """
    policy = APPROVAL_POLICY.get(name)
    card = policy(args) if policy else None
    if card is None:
        return handler(args), None
    if card.get("error"):
        return json.dumps(card), None       # invalid call: no need to ask anybody

    summary = f"{card['action']} {card['target']}"

    def opened(req):
        if report:
            report("approval_required", approval_id=req["id"], name=name, arguments=args,
                   action=card["action"], target=card["target"], summary=summary,
                   timeout_s=TIMEOUT_S)

    approval = request_approval(name, args, card, session_id, channel, on_open=opened)
    approval.update({"tool": name, "summary": summary})
    if report:
        report("approval_resolved", approval_id=approval["id"], name=name, summary=summary,
               decision=approval["decision"], wait_ms=approval["wait_ms"])

    if approval["decision"] != "approved":
        return json.dumps({
            "status": "not_executed", "decision": approval["decision"],
            "action": card["action"], "name": card["target"],
            "message": _NOT_EXECUTED[approval["decision"]],
        }), approval

    result = handler(args)
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data["human_approval"] = f"approved by a human operator after {approval['wait_ms'] / 1000:.1f} s"
            result = json.dumps(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return result, approval


def approval_trigger(approval: dict) -> str:
    """Audit-log trigger for one gated call (informational `note_` prefix)."""
    return (f"note_human_approval: {approval['decision']} · {approval['tool']} · "
            f"{approval['summary']} · {approval['wait_ms'] / 1000:.1f}s")


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/api/approvals")
async def list_pending():
    """Open requests — a reloaded page re-opens its confirmation windows."""
    with _lock:
        return {"pending": [_public(r) for r in _pending.values() if not r["_decision"]]}


@router.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, request: Request):
    body = await request.json()
    decision = {"approve": "approved", "deny": "denied"}.get(body.get("decision"))
    if not decision:
        return JSONResponse({"error": "decision must be 'approve' or 'deny'"}, status_code=400)
    if not resolve(approval_id, decision):
        return JSONResponse({"error": "no such pending approval (already answered or expired)"}, status_code=404)
    return {"id": approval_id, "decision": decision}


@router.get("/api/servers")
async def servers():
    from app.tools.servers import list_servers
    return json.loads(list_servers())


@router.post("/api/servers/reset")
async def servers_reset():
    from app.tools.servers import reset_fleet
    return json.loads(reset_fleet())
