"""The live stream: baseline noise minute by minute, a background scenario every few simulated
hours, visitor-injected scenarios, counters and the per-minute noise strip.

`LiveSim.step()` is synchronous and deterministic given the clock, so tests can drive it with
a fake time function. `run()` is the asyncio loop that publishes to the SSE hub and only lets
the clock run while at least one browser is connected.

Extension points for later milestones:
- `self.filter`: callable(Event) -> bool (True = passed). The rule engine plugs in here; the
  strip then stacks filtered vs passed per minute.
- `self.listeners`: callables(list[Event]) receiving every released batch (triage loop).
- `add_marker(label)`: a labelled marker on the strip where a rule went live.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections import deque
from typing import Any, Callable, Optional

from app.models import Event
from app.sim.catalog import CUSTOMERS, iso
from app.sim.clock import SimClock
from app.sim.generator import _rng, expand_scenario, noise_events, parse_iso
from app.sim.scenarios import SCENARIOS, VISITOR_SCENARIOS
from app.sse import Hub

log = logging.getLogger("signal_desk.live")

STRIP_MINUTES = 30
TAIL_LEN = 10


class InjectRefused(Exception):
    def __init__(self, reason: str, retry_in_s: float):
        super().__init__(reason)
        self.reason = reason
        self.retry_in_s = retry_in_s


class LiveSim:
    def __init__(self, sim_cfg: dict[str, Any], hub: Hub, time_fn: Callable[[], float] = time.monotonic):
        self.cfg = sim_cfg
        self.hub = hub
        self.time_fn = time_fn
        self.seed = sim_cfg["seed"]
        self.start_ts = parse_iso(sim_cfg["live_start"])
        self.rate_mult = float(sim_cfg.get("live_rate_multiplier", 6.0))
        self.auto_every = float(sim_cfg.get("auto_scenario_every_min", 180)) * 60
        self.span_min = float(sim_cfg.get("scenario_span_min", 75))
        self.cooldown_s = float(sim_cfg.get("inject_cooldown_s", 30))
        self.tick_s = float(sim_cfg.get("tick_ms", 250)) / 1000
        self.clock = SimClock(self.start_ts, float(sim_cfg.get("speed", 60)), time_fn)
        self.filter: Optional[Callable[[Event], bool]] = None
        self.listeners: list[Callable[[list[Event]], None]] = []
        self.epoch = 0
        self.reset_state()

    # ------------------------------------------------------------ state

    def reset_state(self) -> None:
        self.epoch += 1
        self.clock.reset(self.start_ts)
        self._pending: list[tuple[float, int, dict]] = []
        self._seq = 0
        self._next_minute = int(self.start_ts // 60 * 60)
        self._next_auto = self.start_ts + 90 * 60          # first background scenario after ~90 s
        self._auto_n = 0
        self._inject_n = 0
        self._ev_n = 0
        self.total = 0
        self.passed_total = 0
        self.filtered_total = 0
        self.buckets: dict[int, list[int]] = {}            # minute epoch -> [passed, filtered]
        self.markers: list[dict[str, Any]] = []
        self.tail: deque[Event] = deque(maxlen=TAIL_LEN)
        self.recent: deque[Event] = deque(maxlen=5000)
        self.inject: Optional[dict[str, Any]] = None       # {id, title, customer, until_sim}
        self._inject_ended_real: Optional[float] = None

    def reset(self) -> None:
        self.reset_state()
        self.hub.publish("reset", self.snapshot())

    def add_marker(self, label: str, ts: Optional[float] = None) -> None:
        self.markers.append({"ts": iso(ts if ts is not None else self.clock.now()), "label": label})

    # ------------------------------------------------------------ scheduling

    def _push(self, events: list[dict]) -> None:
        for e in events:
            self._seq += 1
            heapq.heappush(self._pending, (parse_iso(e["ts"]), self._seq, e))

    def _expand_live(self, sid: str, cust_id: str, key: tuple, incident_id: str, now: float) -> list[dict]:
        """Expand a scenario compressed into span_min, its first event ~30 simulated s from now."""
        sc = SCENARIOS[sid]
        cust = CUSTOMERS[cust_id]
        probe_root = 1_000_000_000.0
        probe = expand_scenario(sc, cust, probe_root, _rng(*key), incident_id, compress_to=self.span_min)
        first = min(parse_iso(e["ts"]) for e in probe)
        root_ts = now + 30 + (probe_root - first)
        return expand_scenario(sc, cust, root_ts, _rng(*key), incident_id, compress_to=self.span_min)

    def _start_auto(self, now: float) -> None:
        # background scenarios: the ones a visitor cannot trigger come first
        order = [s for s in SCENARIOS if not SCENARIOS[s].visitor] + list(VISITOR_SCENARIOS)
        sid = order[self._auto_n % len(order)]
        elig = SCENARIOS[sid].eligible_customers()
        cust = elig[self._auto_n % len(elig)]
        self._auto_n += 1
        inc = f"L{self.epoch}:auto:{sid}#{self._auto_n}"
        self._push(self._expand_live(sid, cust, (self.seed, "auto", sid, self._auto_n), inc, now))

    def inject_state(self) -> dict[str, Any]:
        now_real = self.time_fn()
        st: dict[str, Any] = {"active": None, "title": None, "customer": None, "remaining_s": 0, "cooldown_s": 0}
        if self.inject:
            st.update(active=self.inject["id"], title=self.inject["title"], customer=self.inject["customer"],
                      remaining_s=round(max(0.0, self.clock.real_seconds(self.inject["until_sim"] - self.clock.now())), 1))
        elif self._inject_ended_real is not None:
            st["cooldown_s"] = round(max(0.0, self.cooldown_s - (now_real - self._inject_ended_real)), 1)
        return st

    def start_inject(self, sid: str) -> dict[str, Any]:
        if sid not in VISITOR_SCENARIOS:
            raise KeyError(sid)
        st = self.inject_state()
        if st["active"]:
            raise InjectRefused("busy", st["remaining_s"] + self.cooldown_s)
        if st["cooldown_s"] > 0:
            raise InjectRefused("cooldown", st["cooldown_s"])
        sc = SCENARIOS[sid]
        elig = sc.eligible_customers()
        cust = elig[self._inject_n % len(elig)]
        self._inject_n += 1
        now = self.clock.now()
        inc = f"L{self.epoch}:inject:{sid}#{self._inject_n}"
        events = self._expand_live(sid, cust, (self.seed, "inject", sid, self._inject_n), inc, now)
        until = max(parse_iso(e["ts"]) for e in events)
        self._push(events)
        self.inject = {"id": sid, "title": sc.tile_title, "customer": CUSTOMERS[cust].name, "until_sim": until}
        # keep background scenarios out of the way while a visitor's scenario plays
        self._next_auto = max(self._next_auto, until + 60 * 60)
        st = self.inject_state()
        return {"scenario": sid, "title": sc.tile_title, "customer": CUSTOMERS[cust].name,
                "events": len(events), "plays_for_s": st["remaining_s"]}

    # ------------------------------------------------------------ stepping

    def _finalise(self, e: dict) -> Event:
        self._ev_n += 1
        e = dict(e)
        e.pop("_nid", None)
        e.pop("_picks", None)
        e["id"] = f"L{self.epoch}-{self._ev_n:06d}"
        return Event(**e)

    def step(self) -> list[Event]:
        """Advance to the clock's current time and return the events released since last step."""
        now = self.clock.now()
        while self._next_minute <= now:
            m = self._next_minute
            batch: list[dict] = []
            for cust in CUSTOMERS.values():
                batch.extend(noise_events(cust, m, m + 60, 60, (self.seed, "live"), self.rate_mult))
            self._push(batch)
            self._next_minute += 60
        if now >= self._next_auto:
            if self.inject is None:
                self._start_auto(now)
            self._next_auto = now + self.auto_every
        out: list[Event] = []
        while self._pending and self._pending[0][0] <= now:
            ts, _, raw = heapq.heappop(self._pending)
            ev = self._finalise(raw)
            passed = True if self.filter is None else bool(self.filter(ev))
            minute = int(ts // 60 * 60)
            b = self.buckets.setdefault(minute, [0, 0])
            b[0 if passed else 1] += 1
            self.total += 1
            if passed:
                self.passed_total += 1
            else:
                self.filtered_total += 1
            self.tail.appendleft(ev)
            self.recent.append(ev)
            out.append(ev)
        if self.inject and now >= self.inject["until_sim"]:
            self.inject = None
            self._inject_ended_real = self.time_fn()
        old = int(now // 60 * 60) - (STRIP_MINUTES + 5) * 60
        for k in [k for k in self.buckets if k < old]:
            del self.buckets[k]
        if out:
            for fn in self.listeners:
                try:
                    fn(out)
                except Exception:          # a listener must never stop the stream
                    log.exception("live listener failed")
        return out

    # ------------------------------------------------------------ payloads (no truth, ever)

    def strip(self) -> list[dict[str, Any]]:
        cur = int(self.clock.now() // 60 * 60)
        rows = []
        for k in range(cur - (STRIP_MINUTES - 1) * 60, cur + 60, 60):
            p, f = self.buckets.get(k, [0, 0])
            rows.append({"t": iso(k), "passed": p, "filtered": f})
        return rows

    def tick_payload(self) -> dict[str, Any]:
        return {
            "sim_ts": iso(self.clock.now()), "speed": self.clock.speed, "paused": self.clock.paused,
            "total": self.total, "passed": self.passed_total, "filtered": self.filtered_total,
            "strip": self.strip(), "markers": list(self.markers), "inject": self.inject_state(),
            "clients": self.hub.client_count,
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.tick_payload(), "epoch": self.epoch, "tail": [e.public() for e in self.tail]}

    # ------------------------------------------------------------ asyncio loop

    async def run(self) -> None:
        last_tick = 0.0
        while True:
            try:
                if self.hub.client_count == 0:
                    self.clock.pause()
                    await asyncio.sleep(self.tick_s)
                    continue
                self.clock.resume()
                events = self.step()
                if events:
                    self.hub.publish("events", {"events": [e.public() for e in events]})
                if self.time_fn() - last_tick >= 1.0:
                    last_tick = self.time_fn()
                    self.hub.publish("tick", self.tick_payload())
                await asyncio.sleep(self.tick_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("live loop error")
                await asyncio.sleep(1.0)
