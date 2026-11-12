"""Rule store persistence, the live wiring (filtered share, markers, created events), the rules
API and that no rule payload carries ground truth."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import load_config
from app.engine.runtime import RuleRuntime
from app.engine.store import SEED_RULES, RuleStore
from app.sim.live import LiveSim
from app.sse import Hub, format_sse

SIM = load_config()["sim"]
NTP = {"kind": "suppress", "match": {"source": "checkmk", "title_contains": "NTP Time WARN", "severity_in": ["warning"]},
       "window_min": 1440, "max_per_window": 0}


# ---------------------------------------------------------------- store

def test_store_seeds_persist_toggle_add_reset(tmp_path):
    p = tmp_path / "rules.json"
    s = RuleStore(p)
    assert [r.id for r in s.rules] == [x["id"] for x in SEED_RULES] and p.exists()
    s.toggle(SEED_RULES[0]["id"])
    ar = s.add(NTP, "Checkmk NTP flaps", proposal_id="p1", live_since_ts="2026-11-10T10:00:00Z")
    assert ar.id == "r-checkmk-ntp-flaps" and ar.enabled
    assert s.add(NTP, "Checkmk NTP flaps").id == "r-checkmk-ntp-flaps-2"
    s2 = RuleStore(p)                                    # reload from disk
    assert [r.model_dump() for r in s2.rules] == [r.model_dump() for r in s.rules]
    assert s2.get(SEED_RULES[0]["id"]).enabled is False
    assert s2.get(ar.id).proposal_id == "p1"
    with pytest.raises(ValueError):
        s2.add({"kind": "suppress", "match": {}, "window_min": 5, "max_per_window": 0}, "bad")
    s2.reset_to_seed()
    s3 = RuleStore(p)
    assert [r.id for r in s3.rules] == [x["id"] for x in SEED_RULES] and all(r.enabled for r in s3.rules)
    saved = json.loads(p.read_text())
    assert saved["version"] == 1 and saved["rules"][0]["rule"]["kind"] == "suppress"


def test_store_survives_corrupt_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text("{broken")
    assert len(RuleStore(p).rules) == len(SEED_RULES)


# ---------------------------------------------------------------- live wiring

class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _live(tmp_path):
    ft = FakeTime()
    hub = Hub()
    sim = LiveSim(SIM, hub, time_fn=ft)
    sim.clock.resume()
    rt = RuleRuntime(RuleStore(tmp_path / "rules.json"), sim, hub)
    return sim, rt, ft


def _run(sim, ft, seconds):
    f0, t0 = sim.filtered_total, sim.total
    payloads = []
    for _ in range(seconds):
        ft.t += 1.0
        evs = sim.step()
        payloads.append(format_sse("events", {"events": sim.events_payload(evs)}))
    return (sim.filtered_total - f0) / max(1, sim.total - t0), payloads


def test_live_rule_raises_filtered_share_marker_and_toggle_back(tmp_path):
    sim, rt, ft = _live(tmp_path)
    base, _ = _run(sim, ft, 90)
    ar = rt.activate(NTP, "Checkmk NTP flaps")
    assert sim.markers[-1]["label"] == "Checkmk NTP flaps"
    with_rule, _ = _run(sim, ft, 90)
    assert with_rule > base + 0.05, (base, with_rule)
    assert rt.counts()[ar.id]["filtered"] > 0
    rt.set_enabled(ar.id, False)
    assert sim.markers[-1]["label"].endswith("off")
    off, _ = _run(sim, ft, 90)
    assert off < with_rule - 0.05, (off, with_rule)
    counts_after = rt.counts()[ar.id]["filtered"]
    assert counts_after > 0                              # live count survives the rebuild


def test_drop_figure_without_vs_with_the_rule(tmp_path):
    sim, rt, ft = _live(tmp_path)
    assert sim.tick_payload()["drop"] is None                 # no rule accepted yet
    _run(sim, ft, 15)                                          # 15 real s = 15 simulated minutes
    ar = rt.activate(NTP, "Checkmk NTP flaps")
    d = sim.drop_payload()
    assert d["label"] == "Checkmk NTP flaps" and d["after"] is None      # first minute: measuring
    p0, n0 = sim.passed_alerts_total, rt.counts()[ar.id]["filtered"]
    _run(sim, ft, 12)
    d = sim.drop_payload()
    assert d["after"] < d["before"] and d["pct"] < -10, d
    # same minutes on both sides: with = what reached a human, without = that + what the rule removed
    mins = 12.0
    passed, removed = sim.passed_alerts_total - p0, rt.counts()[ar.id]["filtered"] - n0
    assert removed > 0
    assert d["after"] == round(passed / mins, 1) and d["before"] == round((passed + removed) / mins, 1)
    # the strip counts the same unit (firing alerts), so the level line drawn from these
    # numbers sits on the bars: the minutes since the rule add up to the engine's counts
    assert d["since_ts"] == sim.markers[-1]["ts"]
    since = [b for b in sim.strip() if b["t"] >= d["since_ts"]]
    assert sum(b["passed"] for b in since) == passed
    assert sum(b["filtered"] for b in since) >= removed          # + what the seed rules removed
    rt.set_enabled(ar.id, False)
    assert sim.drop_payload() is None                          # switched off: no claim
    rt.set_enabled(ar.id, True)
    sim.reset()
    assert sim.drop_payload() is None


# The noise rules the engineer usually proposes (shapes as in data/saved_run.json and examples/).
USUAL_NOISE_RULES = {
    "ntp_retune": {"kind": "retune", "source": "checkmk", "check": "ntp_time", "op": ">", "new_threshold": 190.0,
                   "sustain_min": 10},
    "test_vm": {"kind": "suppress", "match": {"source": "zabbix", "host_glob": "*-test-vm-*",
                                              "title_contains": "agent is not available",
                                              "severity_in": ["minor", "warning", "info"]},
                "window_min": 1440, "max_per_window": 0},
    "stale_scom": {"kind": "suppress", "match": {"source": "scom", "host_glob": "*-win-old-*",
                                                 "title_contains": "Health Service unavailable",
                                                 "severity_in": ["warning", "info"]},
                   "window_min": 1440, "max_per_window": 0},
    "zabbix_cpu_retune": {"kind": "retune", "source": "zabbix", "check": "cpu_util", "op": ">",
                          "new_threshold": 90, "sustain_min": 5},
}


@pytest.mark.parametrize("name", sorted(USUAL_NOISE_RULES))
@pytest.mark.parametrize("warm", [5, 40, 150])
def test_each_usual_noise_rule_gives_a_visible_drop(tmp_path, name, warm):
    """Accepting any one usual noise proposal must show a clear dent on Now: the drop figure
    after a dozen simulated minutes is -15 % or better at any point of the demo."""
    sim, rt, ft = _live(tmp_path)
    _run(sim, ft, warm)
    rt.activate(USUAL_NOISE_RULES[name], name)
    _run(sim, ft, 12)
    d = sim.drop_payload()
    assert d["pct"] <= -15, (name, warm, d)


def test_three_noise_rules_cut_the_strip_strongly(tmp_path):
    sim, rt, ft = _live(tmp_path)
    _run(sim, ft, 15)
    p0 = sim.passed_alerts_total
    _run(sim, ft, 30)
    base = (sim.passed_alerts_total - p0) / 30
    for name in ("ntp_retune", "test_vm", "stale_scom"):
        rt.activate(USUAL_NOISE_RULES[name], name)
    _run(sim, ft, 5)
    p0 = sim.passed_alerts_total
    _run(sim, ft, 30)
    after = (sim.passed_alerts_total - p0) / 30
    assert after < 0.65 * base, (base, after)


def test_live_mix_families_fire_on_a_steady_rhythm():
    """A live `mix` family's count over any window is within one of its expected value; the
    history (no mix) keeps its Poisson draw and is byte-identical (test_sim checks determinism)."""
    from app.sim.catalog import CUSTOMERS
    from app.sim.generator import STREAMS, noise_events, parse_iso
    cust = next(c for c in CUSTOMERS.values() if "zabbix" in c.sources)
    s = next(x for x in STREAMS if x.name == "noise:zabbix_test_vm")
    mix = {k: float(v) for k, v in SIM["live_mix"].items()}
    t0 = parse_iso(SIM["live_start"])
    for start_min, mins in ((0, 12), (37, 12), (200, 30)):
        a, b = t0 + start_min * 60, t0 + (start_min + mins) * 60
        evs = noise_events(cust, a, b, 60, (SIM["seed"], "live"), SIM["live_rate_multiplier"], mix)
        firing = [e for e in evs if e["truth"]["pattern"] == s.name and e["status"] == "firing"]
        expected = s.rate * mix[s.name] * mins / 60
        assert abs(len(firing) - expected) <= 1.5, (start_min, len(firing), expected)


def test_live_created_events_are_marked_and_truth_free(tmp_path):
    sim, rt, ft = _live(tmp_path)
    rt.activate({"kind": "precursor", "match": {"source": "checkmk", "title_contains": "NTP Time"}, "count": 2,
                 "within_min": 30, "raise": {"title": "NTP drifting widely", "severity": "minor"}}, "NTP drift")
    rt.activate({"kind": "correlate", "members": [{"source": "zabbix", "title_contains": "agent is not available"},
                                                  {"source": "scom", "title_contains": "Health Service"}],
                 "within_min": 10}, "Agents together")
    _, payloads = _run(sim, ft, 120)
    created = [e for p in payloads for e in json.loads(p[6:])["events"] if e.get("produced_by")]
    assert any(e["children"] for e in created)
    assert {e["produced_by"] for e in created} <= {r.id for r in rt.store.rules}
    for p in payloads:
        assert '"truth"' not in p
    assert '"truth"' not in format_sse("tick", sim.tick_payload())
    assert '"truth"' not in format_sse("hello", sim.snapshot())
    assert '"truth"' not in format_sse("rule_changed", {"rules": rt.payload()})


def test_listeners_receive_survivors_only(tmp_path):
    sim, rt, ft = _live(tmp_path)
    rt.activate(NTP, "Checkmk NTP flaps")
    got = []
    sim.listeners.append(got.extend)
    _run(sim, ft, 60)
    assert got and not any("NTP Time WARN" in e.title for e in got)


# ---------------------------------------------------------------- API

def test_manual_rule_hook_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SIGNAL_DESK_DEV", raising=False)
    with TestClient(main.app) as c:
        c.post("/api/reset")
        r = c.post("/api/rules", json={"title": "Checkmk NTP flaps", "rule": NTP})
        assert r.status_code == 403 and r.json()["error"] == "dev_only"
        assert len(c.get("/api/rules").json()["rules"]) == len(SEED_RULES)
        monkeypatch.setenv("SIGNAL_DESK_DEV", "1")
        monkeypatch.setenv("SIGNAL_DESK_PUBLIC", "1")              # a public deploy never allows it
        assert c.post("/api/rules", json={"title": "x", "rule": NTP}).status_code == 403


def test_rules_api_toggle_activate_reset(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_DEV", "1")
    monkeypatch.delenv("SIGNAL_DESK_PUBLIC", raising=False)
    with TestClient(main.app) as c:
        c.post("/api/reset")
        d = c.get("/api/rules").json()
        assert [r["id"] for r in d["rules"]] == [x["id"] for x in SEED_RULES]
        r0 = d["rules"][0]
        assert r0["description"] and r0["kind"] == "suppress" and r0["enabled"] and "filtered" in r0
        assert '"truth"' not in json.dumps(d)

        t = c.post(f"/api/rules/{r0['id']}/toggle").json()
        assert t["ok"] and t["rule"]["enabled"] is False
        t = c.post(f"/api/rules/{r0['id']}/toggle", json={"enabled": True}).json()
        assert t["rule"]["enabled"] is True
        assert c.post("/api/rules/nope/toggle").status_code == 404

        bad = c.post("/api/rules", json={"title": "x", "rule": {"kind": "suppress", "match": {}, "window_min": 5,
                                                                 "max_per_window": 0}})
        assert bad.status_code == 422 and any("matches too much" in e for e in bad.json()["errors"])
        ok = c.post("/api/rules", json={"title": "Checkmk NTP flaps", "rule": NTP}).json()
        assert ok["ok"] and ok["rule"]["id"].startswith("r-")
        assert main.sim.markers and main.sim.markers[-1]["label"] == "Checkmk NTP flaps"
        assert len(c.get("/api/rules").json()["rules"]) == len(SEED_RULES) + 1

        c.post("/api/reset")
        assert [r["id"] for r in c.get("/api/rules").json()["rules"]] == [x["id"] for x in SEED_RULES]
        assert main.sim.markers == []


def test_activate_rule_function_publishes_rule_changed():
    main.rules.reset()
    q = main.hub.subscribe()
    try:
        ar = main.activate_rule(NTP, "NTP flaps", proposal_id="prop-1")
        msgs = []
        while not q.empty():
            msgs.append(json.loads(q.get_nowait()[6:]))
        changed = [m for m in msgs if m["type"] == "rule_changed"]
        assert changed and changed[-1]["rule_id"] == ar.id
        assert any(r["proposal_id"] == "prop-1" for r in changed[-1]["rules"])
        with pytest.raises(ValueError):
            main.activate_rule({"kind": "retune", "source": "zabbix", "check": "nope", "op": ">", "new_threshold": 1}, "x")
    finally:
        main.hub.unsubscribe(q)
        main.rules.reset()
