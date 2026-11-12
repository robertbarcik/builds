"""Signal Desk: FastAPI app. Health, live alert stream (SSE), scenarios, inject, reset, UI."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from typing import Optional, Union

from app.auth import LoginMiddleware, login_enabled
from app.config import STATIC_DIR, dev_rules_enabled, key_present, load_config, model_ids, public_mode
from app.engine.runtime import RuleRuntime
from app.engine.store import get_store
from app.engineer.agent import EngineerRefused, EngineerService
from app.llm.budget import Budget
from app.llm.client import LLMClient
from app.models import ActiveRule, Rule
from app.sim.catalog import CUSTOMERS
from app.sim.live import InjectRefused, LiveSim
from app.sim.scenarios import SCENARIOS
from app.sse import Hub, format_sse
from app.triage.loop import TriageLoop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signal_desk")

# run.sh passes its own id so it can tell this process from a stale one on the same port
BOOT_ID = os.environ.get("SIGNAL_DESK_BOOT_ID", "").strip() or uuid.uuid4().hex[:12]
STARTED_AT = time.time()

CFG = load_config()
hub = Hub()
sim = LiveSim(CFG["sim"], hub)
rules = RuleRuntime(get_store(), sim, hub)       # filters the live stream via sim.rules_hook
budget = Budget(CFG)                             # every LLM call is metered and capped here
budget.listeners.append(lambda snap: hub.publish("budget", snap))
llm = LLMClient(budget, CFG)                      # shared by triage and the rule engineer
triage = TriageLoop(sim, hub, llm, budget, CFG)   # consumes sim.listeners (rule survivors)


def activate_rule(rule: Union[Rule, dict], title: str, proposal_id: Optional[str] = None) -> ActiveRule:
    """Put a rule live now: validate, store, rebuild the engine, draw a strip marker, publish
    `rule_changed`. Raises ValueError with the validation errors. Used when a proposal is accepted."""
    return rules.activate(rule, title, proposal_id=proposal_id)


# the rule engineer: proposes, the deterministic engine checks, accepting puts a rule live
engineer = EngineerService(llm, hub, CFG, active_fn=lambda: rules.store.enabled(),
                           activate_fn=activate_rule, budget=budget)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(sim.run(), name="live-sim")
    ttask = asyncio.create_task(triage.run(), name="triage")
    log.info("Signal Desk up, boot_id=%s, key_present=%s", BOOT_ID, key_present())
    try:
        yield
    finally:
        engineer.reset()
        for t in (ttask, task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Signal Desk", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(LoginMiddleware)        # no-op unless LOGIN_ENABLED=1 (the deployed box)


@app.exception_handler(Exception)
async def calm_error(request: Request, exc: Exception) -> JSONResponse:
    """Never a traceback in the browser: log it, answer with a short JSON the UI can live with."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"ok": False, "error": "server_error"}, status_code=500)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "boot_id": BOOT_ID,
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "models": model_ids(),
        "key_present": key_present(),
        "public": public_mode(),
        "login": login_enabled(),
        "clients": hub.client_count,
        "sim_ts": sim.tick_payload()["sim_ts"],
    })


@app.get("/api/scenarios")
async def scenarios() -> JSONResponse:
    return JSONResponse({
        "scenarios": [
            {"id": s.id, "title": s.title, "blurb": s.blurb, "visitor": s.visitor,
             "tile_title": s.tile_title or None, "tile_blurb": s.tile_blurb or None}
            for s in SCENARIOS.values()
        ],
        "customers": [{"id": c.id, "name": c.name, "industry": c.industry, "country": c.country}
                      for c in CUSTOMERS.values()],
        "inject": sim.inject_state(),
    })


@app.post("/api/inject")
async def inject(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str((body or {}).get("scenario_id", "")).strip()
    try:
        result = sim.start_inject(sid)
    except KeyError:
        return JSONResponse({"error": "unknown_scenario", "scenario_id": sid}, status_code=404)
    except InjectRefused as e:
        return JSONResponse({"error": e.reason, "retry_in_s": round(e.retry_in_s, 1),
                             "inject": sim.inject_state()}, status_code=409)
    hub.publish("inject", sim.inject_state())
    return JSONResponse({"ok": True, **result})


@app.post("/api/reset")
async def reset() -> JSONResponse:
    rules.reset()                 # seed rules only, fresh engine, live counts zeroed
    sim.reset()
    triage.reset()                # empty board, AI triage back on
    engineer.reset()              # no proposals, no run (the cooldown stays: it guards spend)
    return JSONResponse({"ok": True, "epoch": sim.epoch})


# ------------------------------------------------------------ rule engineer and proposals

def _refused(e: EngineerRefused) -> JSONResponse:
    status = {"busy": 409, "cooldown": 429, "budget": 429, "no_saved_run": 404}.get(e.reason, 409)
    return JSONResponse({"ok": False, "error": e.reason, "retry_in_s": round(e.retry_in_s, 1),
                         "engineer": engineer.state_payload()}, status_code=status)


@app.post("/api/engineer/run")
async def engineer_run() -> JSONResponse:
    try:
        run_id = engineer.start_run()
    except EngineerRefused as e:
        return _refused(e)
    return JSONResponse({"ok": True, "run_id": run_id}, status_code=202)


@app.post("/api/engineer/load-saved")
async def engineer_load_saved() -> JSONResponse:
    try:
        run_id = engineer.load_saved()
    except EngineerRefused as e:
        return _refused(e)
    return JSONResponse({"ok": True, "run_id": run_id}, status_code=202)


@app.get("/api/proposals")
async def proposals() -> JSONResponse:
    return JSONResponse(engineer.snapshot())


@app.post("/api/proposals/{pid}/accept")
async def accept_proposal(pid: str) -> JSONResponse:
    try:
        p, ar = engineer.accept(pid)
    except KeyError:
        return JSONResponse({"error": "unknown_proposal", "id": pid}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": "not_pending", "detail": str(e)}, status_code=409)
    asyncio.create_task(engineer.refresh_pending())     # numbers of the others against the new rule set
    return JSONResponse({"ok": True, "proposal": engineer.proposal_payload(p), "rule": rules.rule_payload(ar)})


@app.post("/api/proposals/{pid}/reject")
async def reject_proposal(pid: str) -> JSONResponse:
    try:
        p = engineer.reject(pid)
    except KeyError:
        return JSONResponse({"error": "unknown_proposal", "id": pid}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": "not_pending", "detail": str(e)}, status_code=409)
    return JSONResponse({"ok": True, "proposal": engineer.proposal_payload(p)})


@app.get("/api/rules")
async def list_rules() -> JSONResponse:
    return JSONResponse({"rules": rules.payload()})


@app.post("/api/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: str, request: Request) -> JSONResponse:
    """Flip a rule on/off, or set it with {"enabled": true|false}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = body.get("enabled") if isinstance(body, dict) else None
    try:
        r = rules.set_enabled(rule_id, None if enabled is None else bool(enabled))
    except KeyError:
        return JSONResponse({"error": "unknown_rule", "rule_id": rule_id}, status_code=404)
    return JSONResponse({"ok": True, "rule": rules.rule_payload(r)})


@app.post("/api/rules")
async def add_rule(request: Request) -> JSONResponse:
    """Developer hook (scripts/try_rule.py --activate): {"title": ..., "rule": {...}}. Off by
    default: config.yaml `dev.manual_rules: true` or SIGNAL_DESK_DEV=1 (never with SIGNAL_DESK_PUBLIC)."""
    if not dev_rules_enabled():
        return JSONResponse({"error": "dev_only",
                             "detail": "manual rules are off; set SIGNAL_DESK_DEV=1 to use this developer hook"},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("rule"), dict):
        return JSONResponse({"error": "bad_request", "errors": ["body must be {\"title\": ..., \"rule\": {...}}"]},
                            status_code=400)
    try:
        r = activate_rule(body["rule"], str(body.get("title") or ""))
    except ValueError as e:
        return JSONResponse({"error": "invalid_rule", "errors": str(e).split("; ")}, status_code=422)
    return JSONResponse({"ok": True, "rule": rules.rule_payload(r)})


@app.get("/api/incidents")
async def incidents() -> JSONResponse:
    return JSONResponse(triage.snapshot())


@app.post("/api/incidents/{inc_id}/dismiss")
async def dismiss_incident(inc_id: str) -> JSONResponse:
    """"Got it": the card leaves the board for good (server-side)."""
    if not triage.dismiss(inc_id):
        return JSONResponse({"error": "unknown_incident", "id": inc_id}, status_code=404)
    return JSONResponse({"ok": True, "id": inc_id, "open": triage.open_count()})


@app.get("/api/budget")
async def budget_view() -> JSONResponse:
    return JSONResponse(budget.snapshot())


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    async def gen():
        q = hub.subscribe()
        try:
            yield format_sse("hello", {"boot_id": BOOT_ID, **sim.snapshot(), **triage.snapshot(),
                                       **engineer.snapshot()})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=10)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
