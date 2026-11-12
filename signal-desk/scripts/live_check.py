"""End-to-end check against a freshly started server (spends real money: a few cents).

    ./run.sh --detach
    .venv/bin/python scripts/live_check.py            # [--url http://127.0.0.1:7870] [--boot-id ID]

Steps, each a PASS/FAIL row:
1. boot      /api/health answers with the boot id of the latest start (logs/server.log, or --boot-id)
             and an API key is present (never printed)
2. stream    the SSE stream says hello (it also keeps the simulator and triage running: both
             pause without a connected browser)
3. inject    "Pull the core uplink" is accepted
4. card      an AI incident card (origin llm) appears within 120 s
5. engineer  a live rule-engineer run finishes with proposals within 200 s
6. accept    the top proposal is accepted and becomes an active rule
7. drop      within 40 s the rule has filtered alerts and, over the minutes since it went live,
             alerts reaching a human per minute with it are fewer than without it (the Now
             screen's drop figure)
Then it prints what the app's own budget says was spent. Exit code 0 only if every row passed.
The demo is reset at the start and left with the accepted rule active.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402


class Stream:
    """Reads /api/stream in a thread and keeps every message (no truth ever arrives here)."""

    def __init__(self, url: str):
        self.url = url
        self.msgs: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error: Optional[str] = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, read=30.0)) as c:
                with c.stream("GET", f"{self.url}/api/stream") as r:
                    for line in r.iter_lines():
                        if self.stop.is_set():
                            return
                        if line.startswith("data: "):
                            with self.lock:
                                self.msgs.append(json.loads(line[6:]))
        except Exception as e:                       # noqa: BLE001
            if not self.stop.is_set():
                self.error = type(e).__name__

    def wait(self, pred: Callable[[dict], bool], timeout: float, since: int = 0) -> Optional[dict]:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for m in self.msgs[since:]:
                    if pred(m):
                        return m
            if self.error:
                return None
            time.sleep(0.25)
        return None

    def mark(self) -> int:
        with self.lock:
            return len(self.msgs)

    def last(self, kind: str) -> Optional[dict]:
        with self.lock:
            return next((m for m in reversed(self.msgs) if m.get("type") == kind), None)


def latest_boot_id() -> Optional[str]:
    try:
        ids = re.findall(r"boot_id=([0-9a-f]{12})", (ROOT / "logs" / "server.log").read_text())
        return ids[-1] if ids else None
    except OSError:
        return None


def main() -> int:
    srv = load_config()["server"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://{srv['host']}:{srv['port']}")
    ap.add_argument("--boot-id", default=None, help="expected boot id (default: the latest in logs/server.log)")
    args = ap.parse_args()
    url = args.url.rstrip("/")
    rows: list[tuple[str, bool, str]] = []

    def row(name: str, ok: bool, detail: str) -> bool:
        rows.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<9} {detail}", flush=True)
        return ok

    http = httpx.Client(base_url=url, timeout=20.0)
    stream: Optional[Stream] = None
    t_start = time.time()
    spent_before = None
    try:
        # 1. boot
        try:
            h = http.get("/api/health").json()
        except Exception as e:                   # noqa: BLE001
            row("boot", False, f"no answer from {url} ({type(e).__name__}); start it with ./run.sh --detach")
            return finish(rows, None, None)
        want = args.boot_id or latest_boot_id()
        ok = h.get("boot_id") == want and h.get("key_present") is True
        if not row("boot", ok, f"boot_id {h.get('boot_id')} (expected {want}), key present: {h.get('key_present')}, "
                               f"up {h.get('uptime_s')} s"):
            return finish(rows, http, None)
        spent_before = http.get("/api/budget").json()["totals"]["cost_usd"]

        # 2. stream
        stream = Stream(url)
        hello = stream.wait(lambda m: m.get("type") == "hello", 10)
        if not row("stream", hello is not None, "hello received" if hello else f"no hello ({stream.error})"):
            return finish(rows, http, spent_before)
        http.post("/api/reset")
        time.sleep(1.0)

        # 3. inject
        mk = stream.mark()
        r = http.post("/api/inject", json={"scenario_id": "core_uplink_down"})
        d = r.json()
        if not row("inject", r.status_code == 200, f"{d.get('title')} at {d.get('customer')}, "
                                                    f"{d.get('events')} events over ~{d.get('plays_for_s')} s"
                   if r.status_code == 200 else f"HTTP {r.status_code}: {d.get('error')}"):
            return finish(rows, http, spent_before)

        # 4. card
        t0 = time.time()
        card = stream.wait(lambda m: m.get("type") == "incident_upsert"
                           and m["incident"].get("origin") == "llm"
                           and m["incident"].get("severity") in ("major", "critical"), 120, mk)
        if card:
            i = card["incident"]
            row("card", True, f"{i['id']} [{i['severity']}] “{i['title']}” after {time.time() - t0:.0f} s")
        else:
            tri = stream.last("triage_state") or {}
            row("card", False, f"no AI card within 120 s (degraded: {tri.get('degraded')}, {tri.get('reason')})")

        # 5. engineer
        mk = stream.mark()
        t0 = time.time()
        r = http.post("/api/engineer/run")
        if r.status_code != 202:
            row("engineer", False, f"refused: HTTP {r.status_code} {r.json().get('error')} "
                                   f"(retry in {r.json().get('retry_in_s')} s)")
            return finish(rows, http, spent_before)
        run_id = r.json()["run_id"]
        end = stream.wait(lambda m: m.get("type") in ("engineer_done", "engineer_error")
                          and m.get("run_id") == run_id, 200, mk)
        props = http.get("/api/proposals").json()
        cost = (props.get("engineer") or {}).get("cost_usd")
        if not end or end["type"] != "engineer_done":
            row("engineer", False, f"run {run_id}: {end.get('reason') if end else 'no result in 200 s'}")
            return finish(rows, http, spent_before)
        row("engineer", True, f"{len(props['proposals'])} proposals in {time.time() - t0:.0f} s, "
                              f"run cost ${cost}: {end['summary']}")

        # 6. accept the top proposal that removes alerts (an early warning adds alerts by design)
        cands = [p for p in props["proposals"] if p["status"] == "proposed"]
        top = next((p for p in cands if p["kind"] != "precursor"), cands[0] if cands else None)
        if top is None:
            row("accept", False, "no proposal to accept")
            return finish(rows, http, spent_before)
        r = http.post(f"/api/proposals/{top['id']}/accept")
        d = r.json()
        rule_id = (d.get("rule") or {}).get("id")
        if not row("accept", r.status_code == 200 and bool(rule_id),
                   f"“{top['title']}” ({top['kind']}, backtest {top['backtest']['alerts_saved']} alerts saved) "
                   f"-> rule {rule_id}"):
            return finish(rows, http, spent_before)

        # 7. drop
        deadline = time.time() + 40
        drop, filtered = None, 0
        while time.time() < deadline:
            time.sleep(2.0)
            tick = stream.last("tick") or {}
            drop = tick.get("drop")
            filtered = ((tick.get("rule_counts") or {}).get(rule_id) or {}).get("filtered", 0)
            if drop and drop.get("after") is not None and drop.get("minutes", 0) >= 8 and filtered > 0:
                break
        ok = bool(drop and drop.get("after") is not None and drop["after"] < drop["before"] and filtered > 0)
        row("drop", ok, (f"reaching a human {drop['before']}/min without the rule -> {drop['after']}/min with it "
                         f"({drop['pct']}%), rule filtered {filtered} alerts") if drop and drop.get("after") is not None
            else f"no drop figure yet (rule filtered {filtered})")
        return finish(rows, http, spent_before)
    finally:
        if stream:
            stream.stop.set()
        print(f"  (took {time.time() - t_start:.0f} s)")


def finish(rows: list[tuple[str, bool, str]], http: Optional[httpx.Client], spent_before: Optional[float]) -> int:
    print("\nLive check")
    print(f"  {'step':<9} result")
    for name, ok, _ in rows:
        print(f"  {name:<9} {'PASS' if ok else 'FAIL'}")
    if http is not None:
        try:
            b = http.get("/api/budget").json()
            t = b["totals"]
            delta = f", this check ${t['cost_usd'] - spent_before:.4f}" if spent_before is not None else ""
            print(f"  budget: {t['calls']} AI calls, ${t['cost_usd']:.4f} since the server started{delta}; "
                  f"${b['usd_last_hour']:.4f} in the last hour")
            for role, v in sorted(t.get("by_role", {}).items()):
                print(f"    {role:<9} {v['calls']} calls  ${v['cost_usd']:.4f}")
        except Exception:                        # noqa: BLE001
            pass
    passed = all(ok for _, ok, _ in rows) and len(rows) == 7
    print(f"  overall: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
