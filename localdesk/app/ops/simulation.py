"""Synthetic ops floor — background telemetry generator.

Emits weighted random infrastructure events (and occasionally files real
incident tickets through the normal create_incident tool) so the demo feels
like a live environment. Default OFF; controlled from the UI.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.db import get_conn
from app.tools.incidents import create_incident
from app.ws_hub import hub

logger = logging.getLogger(__name__)

router = APIRouter()

SERVERS = ["srv-app-01", "srv-app-02", "srv-db-01", "srv-mail-01", "srv-file-01", "vpn-gw-01"]
SITES = ["Bratislava HQ", "Vienna office", "Brno office", "home office"]
SERVICES = ["print-spooler", "ad-sync", "backup-agent", "mail-relay", "monitoring-agent"]

# (type, severity, template, weight, ticket_prob)
EVENT_CATALOG = [
    ("vpn_drop",      "warning",  "VPN tunnel dropped for {user} ({site})",          4, 0.25),
    ("disk_alert",    "warning",  "Disk usage {pct}% on {host}:/var",                2, 0.15),
    ("login_failure", "info",     "Failed login for {user} (attempt {n})",           3, 0.05),
    ("cpu_spike",     "warning",  "CPU at {pct}% on {host} for 5 minutes",           2, 0.10),
    ("latency",       "info",     "p95 latency {ms}ms on {host}",                    3, 0.0),
    ("service_flap",  "critical", "{svc} restarted unexpectedly on {host}",          1, 0.5),
]

STORM_KINDS = {
    "vpn_outage":  {"event": "vpn_drop", "summary": "VPN connection lost"},
    "disk_full":   {"event": "disk_alert", "summary": "Disk capacity alert"},
    "brute_force": {"event": "login_failure", "summary": "Suspicious repeated login failures"},
}

TICKET_CATEGORY = {
    "vpn_drop": "network", "disk_alert": "hardware", "login_failure": "access",
    "cpu_spike": "hardware", "service_flap": "software",
}
TICKET_PRIORITY = {"info": "low", "warning": "medium", "critical": "high"}


def _employee_names() -> list:
    conn = get_conn()
    try:
        return [r[0] for r in conn.execute("SELECT name FROM employees")]
    except Exception:
        return ["Peter Kováč", "Anna Horváthová", "Milan Novák"]
    finally:
        conn.close()


def insert_event(ev: dict) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO events (ts, event_type, severity, source, message, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ev["ts"], ev["event_type"], ev["severity"], ev["source"],
             ev["message"], json.dumps(ev.get("metadata", {}))),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class SimulationEngine:
    def __init__(self):
        self.running = False
        self._task = None
        self._storm = None
        self._last_ticket_ts = 0.0
        self._users = []
        self.events_generated = 0
        self.tickets_filed = 0
        self.on_state_change = None  # set by main.py to tie the sentinel lifecycle

    async def start(self):
        if self.running:
            return
        self.running = True
        self._users = await asyncio.to_thread(_employee_names)
        self._task = asyncio.create_task(self._loop())
        await hub.broadcast("sim_state", {"running": True})
        if self.on_state_change:
            await self.on_state_change(True)
        logger.info("Simulation started")

    async def stop(self):
        if not self.running:
            return
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        await hub.broadcast("sim_state", {"running": False})
        if self.on_state_change:
            await self.on_state_change(False)
        logger.info("Simulation stopped")

    def trigger_storm(self, kind: str = "vpn_outage"):
        if kind not in STORM_KINDS:
            kind = "vpn_outage"
        # Pin a single victim so the pattern is coherent: brute force hits one
        # account, disk_full one host. VPN outages hit many users (unpinned).
        self._storm = {
            "kind": kind,
            "remaining": random.randint(5, 7),
            "user": random.choice(self._users) if (self._users and kind == "brute_force") else None,
            "host": random.choice(SERVERS) if kind == "disk_full" else None,
        }
        logger.info("Storm triggered: %s", kind)

    def _make_event(self) -> dict:
        pinned = {}
        if self._storm and self._storm["remaining"] > 0:
            self._storm["remaining"] -= 1
            storm = self._storm
            if storm["remaining"] == 0:
                self._storm = None
            pinned = {k: storm[k] for k in ("user", "host") if storm.get(k)}
            etype = STORM_KINDS[storm["kind"]]["event"]
            entry = next(e for e in EVENT_CATALOG if e[0] == etype)
        else:
            # rare spontaneous storm keeps a long-running demo alive
            if random.random() < 0.02:
                self.trigger_storm(random.choice(list(STORM_KINDS)))
            entry = random.choices(EVENT_CATALOG, weights=[e[3] for e in EVENT_CATALOG])[0]

        etype, severity, template, _, ticket_prob = entry
        ctx = {
            "user": random.choice(self._users) if self._users else "unknown",
            "site": random.choice(SITES),
            "host": random.choice(SERVERS),
            "svc": random.choice(SERVICES),
            "pct": random.randint(82, 99),
            "ms": random.randint(300, 2400),
            "n": random.randint(3, 9),
        }
        ctx.update(pinned)
        message = template.format(**ctx)
        source = ctx["host"] if "{host}" in template else ctx["user"]
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": etype,
            "severity": severity,
            "source": source,
            "message": message,
            "metadata": {"site": ctx["site"]},
            "ticket_prob": ticket_prob,
            "user": ctx["user"],
        }

    async def _loop(self):
        try:
            while self.running:
                await asyncio.sleep(random.uniform(3.5, 8.0))  # ≤ ~12 events/min
                ev = self._make_event()
                ev["id"] = await asyncio.to_thread(insert_event, ev)
                self.events_generated += 1
                payload = {k: ev[k] for k in ("id", "ts", "event_type", "severity", "source", "message")}
                await hub.broadcast("telemetry_tick", payload)

                if (
                    random.random() < ev["ticket_prob"]
                    and time.time() - self._last_ticket_ts > 20
                ):
                    self._last_ticket_ts = time.time()
                    raw = await asyncio.to_thread(
                        create_incident,
                        summary=ev["message"],
                        priority=TICKET_PRIORITY[ev["severity"]],
                        category=TICKET_CATEGORY.get(ev["event_type"], "other"),
                        reporter_name=ev["user"],
                    )
                    self.tickets_filed += 1
                    await hub.broadcast("incident_created", json.loads(raw))
        except asyncio.CancelledError:
            pass


simulation = SimulationEngine()


@router.post("/api/simulation/start")
async def sim_start():
    await simulation.start()
    return {"running": True}


@router.post("/api/simulation/stop")
async def sim_stop():
    await simulation.stop()
    return {"running": False}


@router.get("/api/simulation/status")
async def sim_status():
    return {
        "running": simulation.running,
        "events_generated": simulation.events_generated,
        "tickets_filed": simulation.tickets_filed,
    }


@router.post("/api/simulation/storm")
async def sim_storm(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind", "vpn_outage")
    if not simulation.running:
        await simulation.start()
    simulation.trigger_storm(kind)
    return {"status": "storm_triggered", "kind": kind}
