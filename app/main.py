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

from app.config import STATIC_DIR, key_present, load_config, model_ids
from app.sim.catalog import CUSTOMERS
from app.sim.live import InjectRefused, LiveSim
from app.sim.scenarios import SCENARIOS
from app.sse import Hub, format_sse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signal_desk")

# run.sh passes its own id so it can tell this process from a stale one on the same port
BOOT_ID = os.environ.get("SIGNAL_DESK_BOOT_ID", "").strip() or uuid.uuid4().hex[:12]
STARTED_AT = time.time()

CFG = load_config()
hub = Hub()
sim = LiveSim(CFG["sim"], hub)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(sim.run(), name="live-sim")
    log.info("Signal Desk up, boot_id=%s, key_present=%s", BOOT_ID, key_present())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Signal Desk", lifespan=lifespan)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "boot_id": BOOT_ID,
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "models": model_ids(),
        "key_present": key_present(),
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
    sim.reset()
    return JSONResponse({"ok": True, "epoch": sim.epoch})


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    async def gen():
        q = hub.subscribe()
        try:
            yield format_sse("hello", {"boot_id": BOOT_ID, **sim.snapshot()})
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
