"""Simulator: determinism, schema validity, designed patterns, truth never leaking, inject cooldown."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.config import load_config
from app.models import SEVERITIES, Event
from app.sim.catalog import CUSTOMERS, SOURCES, TEMPLATES
from app.sim.generator import _rng, expand_scenario, generate_history, parse_iso
from app.sim.live import InjectRefused, LiveSim
from app.sim.scenarios import SCENARIOS, VISITOR_SCENARIOS
from app.sse import Hub, TruthLeak, format_sse

SIM = load_config()["sim"]


def _digest(events: list[dict]) -> str:
    h = hashlib.sha256()
    for e in events:
        h.update(json.dumps(e, sort_keys=True, separators=(",", ":")).encode())
    return h.hexdigest()


@pytest.fixture(scope="module")
def history():
    return generate_history(SIM["seed"], SIM["history_end"], SIM["history_days"])


# ---------------------------------------------------------------- determinism

def test_history_is_deterministic(history):
    events, summary = history
    again, summary2 = generate_history(SIM["seed"], SIM["history_end"], SIM["history_days"])
    assert _digest(events) == _digest(again)
    assert summary == summary2


def test_different_seed_differs():
    a, _ = generate_history(1, SIM["history_end"], 3)
    b, _ = generate_history(2, SIM["history_end"], 3)
    assert _digest(a) != _digest(b)


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_scenario_expansion_is_deterministic(sid):
    sc = SCENARIOS[sid]
    cust = CUSTOMERS[sc.eligible_customers()[0]]
    root = parse_iso("2026-11-05T10:00:00Z")
    one = expand_scenario(sc, cust, root, _rng(7, sid), "x#1")
    two = expand_scenario(sc, cust, root, _rng(7, sid), "x#1")
    assert one == two and len(one) > 0
    c1 = expand_scenario(sc, cust, root, _rng(7, sid), "x#1", compress_to=75)
    c2 = expand_scenario(sc, cust, root, _rng(7, sid), "x#1", compress_to=75)
    assert c1 == c2


# ---------------------------------------------------------------- schema and size

def test_history_size_and_occurrences(history):
    events, summary = history
    assert 35_000 <= len(events) <= 60_000
    for sid, n in summary["scenarios"].items():
        lo, hi = SCENARIOS[sid].occurrences
        assert 4 <= n <= 8 and lo <= n <= hi, (sid, n)
    roots = {e["truth"]["incident_id"] for e in events if e["truth"]["role"] == "root"}
    assert len(roots) == sum(summary["scenarios"].values())


def test_every_event_matches_schema(history):
    events, _ = history
    ids = set()
    for e in events:
        ev = Event(**e)                               # extra="forbid": no stray keys
        assert ev.severity in SEVERITIES
        assert ev.source in SOURCES
        assert ev.native_severity
        assert ev.raw, "vendor raw block missing"
        assert ev.truth is not None
        ids.add(ev.id)
    assert len(ids) == len(events)
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)


def test_catalog_shape():
    assert len(CUSTOMERS) == 3
    assert len(SOURCES) == 20
    for (src, _check), tpl in TEMPLATES.items():
        assert tpl.native in SOURCES[src].severity_map, (src, tpl.check, tpl.native)
    for sc in SCENARIOS.values():
        assert sc.eligible_customers(), sc.id
        for st in sc.steps:
            assert (st.source, st.check) in TEMPLATES, (sc.id, st.source, st.check)
            if st.native:
                assert st.native in SOURCES[st.source].severity_map
    assert len(SCENARIOS) == 8 and len(VISITOR_SCENARIOS) == 3


# ---------------------------------------------------------------- designed patterns

def test_precursors_lead_their_incident(history):
    events, _ = history
    first_pre: dict[str, float] = {}
    root: dict[str, float] = {}
    for e in events:
        inc = e["truth"]["incident_id"]
        if not inc:
            continue
        t = parse_iso(e["ts"])
        if e["truth"]["role"] == "precursor":
            first_pre[inc] = min(first_pre.get(inc, t), t)
        if e["truth"]["role"] == "root":
            root[inc] = min(root.get(inc, t), t)
    with_pre = [i for i in root if i.split("#")[0] in
                {s.id for s in SCENARIOS.values() if any(st.role == "precursor" for st in s.steps)}]
    assert with_pre
    for inc in with_pre:
        assert inc in first_pre and root[inc] - first_pre[inc] >= 10 * 60, inc
    orphans = {e["truth"]["pattern"] for e in events if (e["truth"]["pattern"] or "").startswith("orphan:")}
    assert len(orphans) >= 3


def test_tight_thresholds_are_shallow_and_brief(history):
    events, _ = history
    noise = [e for e in events if e["truth"]["pattern"] == "tight:zabbix_cpu" and e["status"] == "firing"]
    real = [e for e in events if e["check"] == "cpu_util" and e["truth"]["incident_id"] and e["status"] == "firing"]
    assert len(noise) > 1000 and real
    assert max(e["value"] for e in noise) < 90 <= min(e["value"] for e in real)


# ---------------------------------------------------------------- truth never reaches the browser

def test_format_sse_rejects_truth():
    with pytest.raises(TruthLeak):
        format_sse("events", {"events": [{"id": "x", "truth": {"role": "root"}}]})


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _sim():
    ft = FakeTime()
    sim = LiveSim(SIM, Hub(), time_fn=ft)
    sim.clock.resume()
    return sim, ft


def test_live_payloads_have_no_truth():
    sim, ft = _sim()
    sim.start_inject("core_uplink_down")
    seen = 0
    for _ in range(120):
        ft.t += 1.0
        evs = sim.step()
        seen += len(evs)
        msg = format_sse("events", {"events": [e.public() for e in evs]})
        assert '"truth"' not in msg
        assert '"truth"' not in format_sse("tick", sim.tick_payload())
    assert '"truth"' not in format_sse("hello", sim.snapshot())
    assert seen > 200


def test_live_rate_and_inject_events_arrive():
    sim, ft = _sim()
    counts = []
    for _ in range(60):
        ft.t += 1.0
        counts.append(len(sim.step()))
    per_s = sum(counts[5:]) / len(counts[5:])
    assert 3 <= per_s <= 10, per_s
    info = sim.start_inject("backup_disk_fills")
    assert 60 <= info["plays_for_s"] <= 90, info
    got = []
    for _ in range(100):
        ft.t += 1.0
        got.extend(e for e in sim.step() if e.truth and e.truth.scenario_id == "backup_disk_fills")
    roles = {e.truth.role for e in got}
    assert {"precursor", "root", "symptom"} <= roles


# ---------------------------------------------------------------- inject guard

def test_inject_one_at_a_time_and_cooldown():
    sim, ft = _sim()
    with pytest.raises(KeyError):
        sim.start_inject("memory_leak_after_deploy")      # not a visitor tile
    info = sim.start_inject("cert_expiry")
    with pytest.raises(InjectRefused) as busy:
        sim.start_inject("core_uplink_down")
    assert busy.value.reason == "busy"
    for _ in range(int(info["plays_for_s"]) + 2):
        ft.t += 1.0
        sim.step()
    assert sim.inject is None
    with pytest.raises(InjectRefused) as cool:
        sim.start_inject("core_uplink_down")
    assert cool.value.reason == "cooldown" and 0 < cool.value.retry_in_s <= 30
    ft.t += 31
    sim.step()
    assert sim.start_inject("core_uplink_down")["events"] > 30


def test_reset_restores_start():
    sim, ft = _sim()
    for _ in range(10):
        ft.t += 1.0
        sim.step()
    assert sim.total > 0
    sim.reset()
    assert sim.total == 0 and sim.inject is None and abs(sim.clock.now() - sim.start_ts) < 1
