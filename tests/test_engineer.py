"""Rule engineer: tools on a hand-built mini history (exact values), no truth anywhere the model
or the browser can see, output caps, the agent loop with a fake LLM (limits, feedback, server-side
filtering, drafts, saved run) and the proposal endpoints."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.engine.backtest import history_from_dicts
from app.engine.rules import validate
from app.engine.store import seed_rules
from app.engineer import tools as T
from app.engineer.agent import EngineerRefused, EngineerService, rule_identity
from app.llm.budget import BudgetRefused
from app.llm.client import LLMError, ToolCall, ToolTurn, Usage
from app.models import ActiveRule
from app.prompts.engineer import dialect_for, draft_messages, engineer_messages

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp()
VENDOR = {"checkmk": "Checkmk", "zabbix": "Zabbix", "fortigate": "Fortinet FortiGate", "prtg": "Paessler PRTG",
          "netapp": "NetApp", "servicenow": "ServiceNow"}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(n, t, source, check, host, title, sev, native, *, status="firing", value=None, threshold=None,
        role="noise", inc=None):
    return {"id": f"m-{n:04d}", "ts": _iso(t), "customer": "nordhafen", "source": source, "vendor": VENDOR[source],
            "host": host, "service": "svc", "check": check,
            "title": title if status == "firing" else f"Resolved: {title}", "severity": sev,
            "native_severity": native, "status": status, "value": value, "threshold": threshold, "raw": {},
            "truth": {"scenario_id": "s" if inc else None, "role": role, "incident_id": inc,
                      "pattern": None if inc else "noise:mini"}}


def mini_rows() -> list[dict]:
    rows, n = [], 0
    H = 3600

    def add(*a, **k):
        nonlocal n
        n += 1
        rows.append(_ev(n, *a, **k))

    for i, v in enumerate((160, 170, 180, 190, 200, 210)):          # NTP flaps, 02:00-07:00 CET
        host, t = f"nhl-win-0{i % 3 + 1}", T0 + (i + 1) * H
        title = f"NTP Time WARN on {host}: offset {v} ms"
        add(t, "checkmk", "ntp_time", host, title, "warning", "WARN", value=v, threshold=150)
        add(t + 180, "checkmk", "ntp_time", host, title, "warning", "OK", status="resolved", value=v, threshold=150)
    for i in range(4):                                                  # test VM
        t = T0 + 1.5 * H + i * H
        title = "Zabbix agent is not available (for 3m)"
        add(t, "zabbix", "agent_unavailable", "nhl-test-vm-03", title, "minor", "Average")
        add(t + 120, "zabbix", "agent_unavailable", "nhl-test-vm-03", title, "minor", "Resolved", status="resolved")
    for t in (T0 + 8 * H, T0 + 9 * H):                                  # VPN blip, two tools
        add(t, "fortigate", "vpn_down", "nhl-fw-01", "IPsec VPN tunnel to-branch-01 down", "warning", "warning")
        add(t + 60, "prtg", "ping", "nhl-br-kiel-rt01", "Ping Down on nhl-br-kiel-rt01", "critical", "Down")
        add(t + 300, "fortigate", "vpn_down", "nhl-fw-01", "IPsec VPN tunnel to-branch-01 down", "warning", "warning",
            status="resolved")
        add(t + 360, "prtg", "ping", "nhl-br-kiel-rt01", "Ping Down on nhl-br-kiel-rt01", "critical", "Up",
            status="resolved")
    r = T0 + 20 * H                                                     # incident A: backup repository
    nf = "Volume Space Nearly Full on vol_veeam_repo01"
    add(r - 300 * 60, "netapp", "vol_nearly_full", "nhl-nas-01", nf, "warning", "Warning", value=87.0, threshold=85,
        role="precursor", inc="A")
    add(r - 120 * 60, "netapp", "vol_nearly_full", "nhl-nas-01", nf, "warning", "Warning", value=93.0, threshold=85,
        role="precursor", inc="A")
    add(r, "netapp", "vol_full", "nhl-nas-01", "Volume Space Full on vol_veeam_repo01", "major", "Error", value=99.0,
        threshold=98, role="root", inc="A")
    add(r + 30 * 60, "servicenow", "incident", "nhl-vbr-01", "Nightly backups failed: repository volume full", "major",
        "2 - High", role="symptom", inc="A")
    for h in (30, 32):                                                  # the same warning, leading nowhere
        add(T0 + h * H, "netapp", "vol_nearly_full", "nhl-nas-01", nf, "warning", "Warning", value=86.0, threshold=85)
    b = T0 + 40 * H                                                     # incident B: one host down, no ticket
    add(b, "zabbix", "icmp_unavailable", "nhl-app-01", "Unavailable by ICMP ping", "major", "High", role="root", inc="B")
    add(b + 600, "zabbix", "icmp_unavailable", "nhl-app-01", "Unavailable by ICMP ping", "major", "Resolved",
        status="resolved", role="root", inc="B")
    return rows


@pytest.fixture()
def hist():
    return history_from_dicts(mini_rows())


@pytest.fixture()
def ctx(hist):
    return T.ToolContext(active=[], hist=hist)


NTP_SUPPRESS = {"kind": "suppress", "match": {"source": "checkmk", "title_contains": "NTP Time WARN",
                                              "severity_in": ["warning"]}, "window_min": 60, "max_per_window": 0}
TESTVM = {"kind": "suppress", "match": {"source": "zabbix", "host_glob": "*-test-vm-*",
                                        "title_contains": "agent is not available", "severity_in": ["minor"]},
          "window_min": 1440, "max_per_window": 0}
NETAPP_PRE = {"kind": "precursor", "match": {"source": "netapp", "title_contains": "Volume Space Nearly Full"},
              "count": 2, "within_min": 720, "raise": {"title": "Backup repository filling up", "severity": "major"}}
HIDES_B = {"kind": "suppress", "match": {"source": "zabbix", "title_contains": "Unavailable by ICMP",
                                         "severity_in": ["major"]}, "window_min": 60, "max_per_window": 0}
SAVES_NOTHING = {"kind": "suppress", "match": {"source": "zabbix", "title_contains": "has been restarted",
                                               "severity_in": ["warning"]}, "window_min": 60, "max_per_window": 0}
TOO_LATE = {"kind": "precursor", "match": {"source": "netapp", "title_contains": "Volume Space Full"},
            "count": 1, "within_min": 60, "raise": {"title": "Backup repository full", "severity": "major"}}

ALL_CALLS = [
    ("top_alerts", {}), ("top_alerts", {"group_by": "host"}), ("incident_tickets", {}),
    ("timeline", {"match": {"source": "checkmk", "title_contains": "NTP"}, "bucket": "hour_of_day"}),
    ("timeline", {"match": {"source": "netapp"}, "bucket": "day"}),
    ("co_occurrence", {"match": {"source": "fortigate", "title_contains": "IPsec VPN"}, "within_min": 5}),
    ("preceded_by", {"match": {"source": "servicenow", "title_contains": "repository volume full"},
                     "lookback_min": 720}),
    ("value_distribution", {"source": "checkmk", "check": "ntp_time"}),
    ("backtest", {"rule": NTP_SUPPRESS, "title": "NTP"}), ("backtest", {"rule": NETAPP_PRE}),
    ("backtest", {"rule": {"kind": "suppress", "match": {"source": "checkmk"}, "window_min": 5,
                           "max_per_window": 0}}),
]


# =============================================================== tools: exact values

def test_top_alerts(ctx):
    out, line = T.run_tool("top_alerts", {}, ctx)
    assert out["ok"] and out["alerts_total"] == 21
    assert out["rows"][0].startswith("checkmk/ntp_time | 6 28.6% | warning |")
    assert "3 hosts, top 3 = 100% (nhl-win-01, nhl-win-02, nhl-win-03)" in out["rows"][0]
    assert "open ~3m" in out["rows"][0]
    agent = next(r for r in out["rows"] if r.startswith("zabbix/agent_unavailable"))
    assert agent.startswith("zabbix/agent_unavailable | 4 19.0% | minor |") and "1 hosts" in agent
    assert "Checkmk “ntp_time” with 6 (28.6%)" in line
    hosts, _ = T.run_tool("top_alerts", {"group_by": "host", "limit": 1}, ctx)
    assert hosts["rows"] == ["nhl-nas-01 | 5 (23.8%) | mostly netapp/vol_nearly_full 4, netapp/vol_full 1"]


def test_top_alerts_after_active_rules(hist):
    out, _ = T.run_tool("top_alerts", {}, T.ToolContext(active=[NTP_SUPPRESS], hist=hist))
    assert out["alerts_total"] == 15 and not any(r.startswith("checkmk/ntp_time") for r in out["rows"])


def test_incident_tickets(ctx):
    out, line = T.run_tool("incident_tickets", {}, ctx)
    assert out["tickets_total"] == 1
    assert out["rows"] == ['2 - High | "Nightly backups failed: repository volume full" | 1x | nordhafen 10-10 21:30']
    assert line == "Read the 1 incident tickets of the month (1 kinds)"


def test_timeline(ctx):
    out, _ = T.run_tool("timeline", {"match": {"source": "checkmk", "title_contains": "NTP"}}, ctx)
    assert out["matched"] == 6 and out["bucket"] == "hour_of_day"
    assert "01:0 02:1 03:1 04:1 05:1 06:1 07:1 08:0" in out["counts"]
    day, _ = T.run_tool("timeline", {"match": {"source": "netapp"}, "bucket": "day"}, ctx)
    assert day["counts"] == "d1:3 d2:2"          # days counted from the first event of the history


def test_co_occurrence(ctx):
    out, line = T.run_tool("co_occurrence", {"match": {"source": "fortigate", "title_contains": "IPsec VPN"},
                                             "within_min": 5}, ctx)
    assert out["anchors"] == 2
    assert out["rows"][0].startswith("prtg/ping | with 2 of 2 (100.0%) | lift ")
    assert "median gap +1 min | same host 0.0%" in out["rows"][0]
    assert "Paessler PRTG “ping” comes with 2 of 2" in line


def test_preceded_by(ctx):
    out, _ = T.run_tool("preceded_by", {"match": {"source": "servicenow", "title_contains": "repository volume full"},
                                        "lookback_min": 720}, ctx)
    assert out["anchors"] == 1 and out["min_lead_min"] == 60
    row = next(r for r in out["rows"] if r.startswith("netapp/vol_nearly_full"))
    assert "before 1 of 1 | median lead 330 min | typically 2 in the window" in row
    assert "50.0% of its 4 alerts were followed by one" in row
    # judged over the lookback (12 h), the horizon a rule would set with horizon_h
    assert "horizon_h 12: 2 warnings, 1 followed by one 60 min to 12 h later" in row
    # the root alert 30 min before the ticket is the failure itself, not a warning
    assert not any(r.startswith("netapp/vol_full") for r in out["rows"])


def test_value_distribution(ctx):
    out, line = T.run_tool("value_distribution", {"source": "checkmk", "check": "ntp_time"}, ctx)
    assert out["alerts"] == 6 and out["current_threshold"] == "> 150" and out["max"] == 210
    assert out["percentiles"]["p50"] == 185.0 and out["percentiles"]["p10"] == 165.0
    assert out["open_minutes"] == {"median": 3.0, "share_cleared_within_5_min_pct": 100.0}
    assert out["alerts_kept_if_threshold"]["> 160"] == 5 and out["alerts_kept_if_threshold"]["> 185"] == 3
    assert "median 185ms" in line
    bad, _ = T.run_tool("value_distribution", {"source": "checkmk", "check": "interface"}, ctx)
    assert not bad["ok"] and "no numeric value" in bad["error"]


def test_backtest_tool(ctx):
    out, line = T.run_tool("backtest", {"rule": NTP_SUPPRESS, "title": "silence NTP"}, ctx)
    assert out["ok"] and out["alerts_saved"] == 6 and out["saved_pct"] == 28.6
    assert out["incidents_still_visible"] == "2 of 2" and out["incidents_hidden"] == 0
    assert line == "Backtested: silence NTP → 6 alerts saved (28.6%), 0 incidents hidden"
    pre, pline = T.run_tool("backtest", {"rule": NETAPP_PRE}, ctx)
    assert (pre["early_warnings"], pre["warnings_followed_by_incident"], pre["false_alarms"]) == (2, 1, 1)
    assert pre["median_lead_min"] == 120.0 and pre["alerts_saved"] == -2
    assert "2 early warnings, 1 right, 1 false alarms" in pline


def test_backtest_errors_come_back_as_short_strings(ctx):
    out, line = T.run_tool("backtest", {"rule": {"kind": "suppress", "match": {"source": "checkmk"},
                                                 "window_min": 5, "max_per_window": 0}}, ctx)
    assert out["ok"] is False and out["errors"] == ["suppress could drop critical alerts: add severity_in without 'critical'"]
    assert line.startswith("Backtest refused the rule:")
    out2, _ = T.run_tool("backtest", {"rule": {"kind": "retune", "source": "nagios", "check": "x", "op": ">",
                                               "new_threshold": 1}}, ctx)
    assert not out2["ok"] and "not a known source" in out2["errors"][0]


def test_bad_arguments_never_raise(ctx):
    for name, args in [("timeline", {}), ("timeline", {"match": {"bogus": 1}}), ("co_occurrence", {"match": {}}),
                       ("preceded_by", {"match": {"source": "nope"}}), ("top_alerts", {"limit": 99}),
                       ("nope", {}), ("top_alerts", "not a dict")]:
        out, line = T.run_tool(name, args, ctx)
        assert out["ok"] is False and line


# =============================================================== no truth, size cap

def test_no_truth_in_any_tool_output(ctx):
    for name, args in ALL_CALLS:
        out, line = T.run_tool(name, args, ctx)
        text = T.dumps(out) + line
        for bad in ('"truth"', "scenario_id", "incident_id", '"role"', '"pattern"', "noise:mini", "precursor\"",
                    "symptom"):
            assert bad not in text, (name, bad)


def test_assert_clean_catches_leaks():
    with pytest.raises(T.TruthLeak):
        T.assert_clean({"rows": [{"truth": {"role": "noise"}}]})
    with pytest.raises(T.TruthLeak):
        T.assert_clean({"rows": ["checkmk/ntp_time pattern noise:checkmk_ntp"]})
    T.assert_clean({"rows": ["fine"]})


def test_tool_outputs_are_capped(ctx):
    for name, args in ALL_CALLS:
        out, _ = T.run_tool(name, args, ctx)
        assert len(T.dumps(out)) <= T.MAX_OUTPUT_CHARS
    big = {"ok": True, "rows": ["x" * 150 for _ in range(40)]}
    capped = T.cap_output(big)
    assert capped["truncated"] is True and len(T.dumps(capped)) <= T.MAX_OUTPUT_CHARS and len(capped["rows"]) < 40


def test_prompt_is_generated_and_truth_free():
    msgs = engineer_messages(14, [{"title": "Firewall admin logins", "description": "Hides info FortiGate alerts."}])
    text = json.dumps(msgs)
    for bad in ("truth", "scenario_id", "noise:", "tight:", "orphan:", "incident_id"):
        assert bad not in text
    from app.engine.rules import KINDS
    from app.sim.catalog import SOURCES, TEMPLATES
    for sid in SOURCES:
        assert f"- {sid} (" in msgs[0]["content"]
    for (src, chk) in TEMPLATES:
        assert chk in msgs[0]["content"]
    for k in KINDS:
        assert k in msgs[0]["content"]
    assert "at most 14 tool calls" in msgs[0]["content"] and "Firewall admin logins" in msgs[1]["content"]


def test_dialect_by_source():
    def d(rule):
        return dialect_for(validate(rule)[0])[0]
    assert d(NTP_SUPPRESS) == "checkmk" and d(TESTVM) == "zabbix"
    assert d({"kind": "retune", "source": "alertmanager", "check": "CPUThrottlingHigh", "op": ">",
              "new_threshold": 50}) == "prometheus"
    assert d({"kind": "retune", "source": "azure_monitor", "check": "cpu_pct", "op": ">", "new_threshold": 85}) == "azure_kql"
    assert d(NETAPP_PRE) == "generic"
    assert d({"kind": "correlate", "members": [{"source": "fortigate", "title_contains": "IPsec"},
                                               {"source": "prtg", "title_contains": "Ping Down"}],
              "within_min": 5, "keep": 0}) == "generic"
    dialect, msgs = draft_messages(validate(TESTVM)[0], "Test VM", {"title": "x", "raw": {"eventid": "1"}})
    assert dialect == "zabbix" and "eventid" in msgs[1]["content"] and "truth" not in json.dumps(msgs)


def test_sample_alert_has_no_truth(hist):
    s = T.sample_alert(validate(NTP_SUPPRESS)[0], hist)
    assert s["raw"]["HOSTNAME"].startswith("nhl-win") and "truth" not in json.dumps(s)


# =============================================================== agent loop with a fake LLM

class Hub:
    def __init__(self, clients=1):
        self.client_count = clients
        self.msgs: list[tuple[str, dict]] = []

    def publish(self, kind, data):
        json.dumps(data)                      # must be serialisable, like the real hub
        assert "truth" not in json.dumps(data)
        self.msgs.append((kind, data))

    def kinds(self, kind):
        return [d for k, d in self.msgs if k == kind]


def _turn(calls=(), content=""):
    tcs = [ToolCall(id=f"c{i}-{name}", name=name, arguments=args, raw_arguments=json.dumps(args))
           for i, (name, args) in enumerate(calls)]
    msg = {"role": "assistant", "content": content or None}
    if tcs:
        msg["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.raw_arguments}}
                             for t in tcs]
    return ToolTurn(message=msg, content=content, tool_calls=tcs, finish_reason="tool_calls",
                    usage=Usage(1000, 100, 0.003, "fake"))


class FakeLLM:
    """Scripted engineer: `script` is a list of turns (or a callable(messages, n) -> turn)."""

    def __init__(self, script, draft="groups: []", draft_fail=False, delay=0.0):
        self.script = script
        self.n = 0
        self.seen: list[list[dict]] = []
        self.draft = draft
        self.draft_fail = draft_fail
        self.delay = delay
        self.json_calls = 0

    def role_spec(self, role):
        return {"timeout_s": 60}

    async def tool_call(self, role, messages, tools, **kw):
        assert role == "engineer" and kw.get("tool_choice") == "auto"
        self.seen.append(json.loads(json.dumps(messages)))
        if self.delay:
            await asyncio.sleep(self.delay)
        self.n += 1
        if callable(self.script):
            t = self.script(messages, self.n)
        else:
            t = self.script[min(self.n, len(self.script)) - 1]
        if isinstance(t, Exception):
            raise t
        return t

    async def json_call(self, role, messages, **kw):
        assert role == "render"
        self.json_calls += 1
        if self.draft_fail:
            raise LLMError("server", "boom")
        return {"text": self.draft}, Usage(100, 50, 0.0001, "fake")


def _prop(rule, title="A rule", reasoning="Because the data says so.", **extra):
    return {"title": title, "reasoning": reasoning, "rule": rule, **extra}


def _service(llm, hist, tmp_path, *, active=(), clients=1, cfg_caps=None, clock=None, activate=None):
    cfg = json.loads(json.dumps(load_config()))
    cfg["caps"].update(cfg_caps or {})
    activated = []

    def act(rule, title, proposal_id=None):
        activated.append((rule, title, proposal_id))
        return ActiveRule(id=f"r-{len(activated)}", title=title, rule=validate(rule if isinstance(rule, dict) else
                                                                                rule.model_dump(by_alias=True))[0])

    svc = EngineerService(llm, Hub(clients), cfg, active_fn=lambda: list(active), activate_fn=activate or act,
                          history=hist, saved_path=tmp_path / "saved_run.json",
                          time_fn=clock or time.monotonic, sleep=lambda s: asyncio.sleep(0))
    svc.activated = activated
    return svc


def _run(svc, start="run"):
    async def go():
        (svc.start_run if start == "run" else svc.load_saved)()
        await svc.task
    asyncio.run(go())


GOOD_SCRIPT = [
    _turn([("top_alerts", {}), ("incident_tickets", {})], "Start with the biggest noise."),
    _turn([("backtest", {"rule": NTP_SUPPRESS, "title": "NTP"}), ("backtest", {"rule": NETAPP_PRE})]),
    _turn([("submit_proposals", {"proposals": [_prop(NTP_SUPPRESS, "Silence NTP flaps"),
                                               _prop(NETAPP_PRE, "Backup repository filling up")]})]),
]


def test_good_run_streams_log_proposals_done_and_saves(hist, tmp_path):
    llm = FakeLLM(GOOD_SCRIPT)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    assert svc.state == "done", svc.error
    log = [e["entry"]["text"] for e in svc.hub.kinds("engineer_log")]
    assert "Start with the biggest noise." in log
    assert any(t.startswith("Backtested: NTP → 6 alerts saved") for t in log)
    props = svc.hub.kinds("engineer_proposal")
    assert [p["proposal"]["title"] for p in props] == ["Silence NTP flaps", "Backup repository filling up"]
    assert props[0]["proposal"]["native_draft"] == {"dialect": "checkmk", "label": "Illustrative draft",
                                                    "text": "groups: []"}
    assert props[0]["proposal"]["description"] == "Hides warning Checkmk alerts containing “NTP Time WARN”."
    done = svc.hub.kinds("engineer_done")[0]
    assert done["summary"] == ("2 rules proposed; together they would have removed 19% of last month's alerts "
                               "and hidden none of the 2 incidents.")      # (6 - 2) / 21, from one combined replay
    saved = json.loads((tmp_path / "saved_run.json").read_text())
    assert saved["date"] and len(saved["proposals"]) == 2 and saved["log"] and saved["tool_calls"] == 5
    assert "truth" not in json.dumps(saved)
    # the run's one-off cost travels with the run (3 engineer turns + 2 drafts), not in "per minute"
    assert svc.state_payload()["cost_usd"] == pytest.approx(3 * 0.003 + 2 * 0.0001, abs=1e-4)


def test_tool_call_cap(hist, tmp_path):
    many = _turn([("top_alerts", {}), ("top_alerts", {"group_by": "host"}), ("incident_tickets", {})])
    llm = FakeLLM(lambda msgs, n: many)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    executed = [e for e in svc.hub.kinds("engineer_log") if e["entry"]["kind"] == "tool"]
    assert len(executed) == svc.max_calls - 1            # the last call is reserved for the submit
    assert svc.state == "failed" and svc.error["reason"] == "no_proposals"
    last = llm.seen[-1]
    assert any(m["role"] == "user" and "submit_proposals now" in (m["content"] or "") for m in last)
    refused = [m for m in last if m["role"] == "tool" and "tool budget used up" in m["content"]]
    assert refused


def test_forced_submit_still_accepted_after_cap(hist, tmp_path):
    def script(msgs, n):
        if any(m["role"] == "user" and "Time is up" in (m["content"] or "") for m in msgs):
            return _turn([("submit_proposals", {"proposals": [_prop(NTP_SUPPRESS), _prop(TESTVM)]})])
        return _turn([("top_alerts", {}), ("incident_tickets", {}), ("top_alerts", {"group_by": "host"})])
    svc = _service(FakeLLM(script), hist, tmp_path)
    _run(svc)
    assert svc.state == "done" and len(svc.proposals) == 2


def test_timeout(hist, tmp_path):
    llm = FakeLLM(GOOD_SCRIPT, delay=2.0)
    svc = _service(llm, hist, tmp_path, cfg_caps={"engineer_max_seconds": 0.3})
    _run(svc)
    assert svc.state == "failed" and svc.error["reason"] == "timeout"
    assert svc.hub.kinds("engineer_error")[0]["reason"] == "timeout"


def test_single_run_and_cooldown(hist, tmp_path):
    now = [1000.0]
    llm = FakeLLM(GOOD_SCRIPT, delay=0.05)
    svc = _service(llm, hist, tmp_path, clock=lambda: now[0], cfg_caps={"engineer_cooldown_s": 90})

    async def go():
        svc.start_run()
        with pytest.raises(EngineerRefused) as busy:
            svc.start_run()
        assert busy.value.reason == "busy"
        with pytest.raises(EngineerRefused) as busy2:
            svc.load_saved()
        assert busy2.value.reason == "busy"
        await svc.task
        with pytest.raises(EngineerRefused) as cool:
            svc.start_run()
        assert cool.value.reason == "cooldown" and 0 < cool.value.retry_in_s <= 90
        now[0] += 91
        llm.n = 0
        svc.start_run()
        await svc.task
    asyncio.run(go())
    assert svc.state == "done"


def test_budget_refusal_is_reported_and_costs_no_cooldown(hist, tmp_path):
    llm = FakeLLM([BudgetRefused("usd_per_hour_session", 600)])
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    assert svc.state == "failed" and svc.error["reason"] == "budget"
    assert svc.cooldown_left() == 0


def test_empty_output_retried_once(hist, tmp_path):
    seq = [LLMError("empty", "nothing")] + GOOD_SCRIPT
    llm = FakeLLM(seq)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    assert svc.state == "done" and llm.n == 4
    llm2 = FakeLLM([LLMError("empty", "x"), LLMError("empty", "y")])
    svc2 = _service(llm2, hist, tmp_path)
    _run(svc2)
    assert svc2.state == "failed" and svc2.error["reason"] == "empty"


def test_invalid_rule_is_fed_back_and_the_loop_continues(hist, tmp_path):
    bad = {"kind": "suppress", "match": {"source": "checkmk", "title_contains": "NTP"}, "window_min": 60,
           "max_per_window": 0, "colour": "red"}
    script = [
        _turn([("backtest", {"rule": bad})]),
        _turn([("backtest", {"rule": NTP_SUPPRESS})]),
        _turn([("submit_proposals", {"proposals": [_prop(NTP_SUPPRESS), _prop(TESTVM)]})]),
    ]
    llm = FakeLLM(script)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    fed = json.loads(tool_msgs[0]["content"])
    assert fed["ok"] is False and any("unknown key 'colour'" in e for e in fed["errors"])
    assert svc.state == "done" and len(svc.proposals) == 2


def test_invalid_submit_is_sent_back_once(hist, tmp_path):
    bad = {"kind": "suppress", "match": {"source": "checkmk"}, "window_min": 60, "max_per_window": 0}
    script = [
        _turn([("submit_proposals", {"proposals": [_prop(bad, "Bad"), _prop(NTP_SUPPRESS)]})]),
        _turn([("submit_proposals", {"proposals": [_prop(NTP_SUPPRESS), _prop(TESTVM)]})]),
    ]
    llm = FakeLLM(script)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    back = json.loads([m for m in llm.seen[1] if m["role"] == "tool"][0]["content"])
    assert back["ok"] is False and "proposal 1 (Bad)" in back["errors"][0]
    assert svc.state == "done" and [p.rule.match.source for p in svc.proposals] == ["checkmk", "zabbix"]


def test_server_side_filtering_and_model_numbers_ignored(hist, tmp_path):
    active = [ActiveRule(id="seed-x", title="NTP", rule=validate(NTP_SUPPRESS)[0])]
    ntp_variant = json.loads(json.dumps(NTP_SUPPRESS))
    ntp_variant["window_min"] = 1440                       # same rule, other tuning: a duplicate
    cands = [
        _prop(HIDES_B, "Hides an incident"),
        _prop(SAVES_NOTHING, "Saves nothing"),
        _prop(NETAPP_PRE, "Backup repository filling up",
              backtest={"alerts_saved": 999999, "incidents_hidden": 0}, alerts_saved=999999),
        _prop(ntp_variant, "NTP again"),
        _prop(TESTVM, "Test VM"),
        _prop(TOO_LATE, "Sixth: never looked at (max 5)"),
    ]
    svc = _service(FakeLLM([]), hist, tmp_path, active=active)

    async def go():
        return await svc._finalise(cands, active, origin="live")
    keep = asyncio.run(go())
    assert [p.title for p in keep] == ["Test VM", "Backup repository filling up"]
    pre = keep[1]
    assert pre.backtest.alerts_saved == -2 and pre.backtest.precursor_true == 1     # the engine's numbers
    dropped = " ".join(svc.stats["dropped"])
    assert "Hides an incident: hides 1 incident" in dropped
    assert "Saves nothing: saves only 0% of the alerts" in dropped
    assert "NTP again: already active or proposed" in dropped
    assert "Sixth" not in dropped
    # Test VM was cut from 15 alerts (NTP already active): 4 of 15
    assert keep[0].backtest.alerts_before == 15 and keep[0].backtest.alerts_saved == 4
    late = asyncio.run(svc._finalise([_prop(TOO_LATE, "Warns at the failure")], active, origin="live"))
    assert late == [] and "Warns at the failure: warns too late" in svc.stats["dropped"][0]


def test_duplicate_within_one_submission(hist, tmp_path):
    svc = _service(FakeLLM([]), hist, tmp_path)
    keep = asyncio.run(svc._finalise([_prop(TESTVM, "one"), _prop(TESTVM, "two")], [], origin="live"))
    assert [p.title for p in keep] == ["one"]
    assert rule_identity(validate(TESTVM)[0]) == rule_identity(validate({**TESTVM, "window_min": 5})[0])


def test_render_failure_keeps_the_proposal(hist, tmp_path):
    llm = FakeLLM(GOOD_SCRIPT, draft_fail=True)
    svc = _service(llm, hist, tmp_path)
    _run(svc)
    assert svc.state == "done" and len(svc.proposals) == 2
    assert all(p.native_draft is None for p in svc.proposals) and llm.json_calls == 2


def test_no_survivors_fails_cleanly(hist, tmp_path):
    script = [_turn([("submit_proposals", {"proposals": [_prop(HIDES_B), _prop(SAVES_NOTHING)]})])]
    svc = _service(FakeLLM(script), hist, tmp_path)
    _run(svc)
    assert svc.state == "failed" and svc.error["reason"] == "no_proposals"
    assert not (tmp_path / "saved_run.json").exists()


def test_abandoned_run_is_cancelled(hist, tmp_path, monkeypatch):
    import app.engineer.agent as A
    monkeypatch.setattr(A, "ABANDON_AFTER_S", 1.0)
    svc = _service(FakeLLM(GOOD_SCRIPT, delay=5.0), hist, tmp_path, clients=0)
    _run(svc)
    assert svc.state == "failed" and svc.error["reason"] == "cancelled"


# =============================================================== saved run, accept, reject, reset

def test_saved_run_round_trip(hist, tmp_path):
    svc = _service(FakeLLM(GOOD_SCRIPT), hist, tmp_path)
    _run(svc)
    first = [(p.title, p.backtest.alerts_saved) for p in svc.proposals]
    # a fresh service (e.g. after a restart) with the NTP rule now active, and no network at all
    active = [ActiveRule(id="r-ntp", title="NTP", rule=validate(NTP_SUPPRESS)[0])]
    llm2 = FakeLLM([LLMError("connection", "offline")])
    svc2 = _service(llm2, hist, tmp_path, active=active)
    _run(svc2, start="saved")
    assert svc2.state == "done" and svc2.mode == "saved" and svc2.saved_date == json.loads(
        (tmp_path / "saved_run.json").read_text())["date"]
    assert llm2.n == 0 and llm2.json_calls == 0                      # replay needs no model
    assert [p.title for p in svc2.proposals] == ["Backup repository filling up"]   # NTP is active now
    assert all(p.origin == "saved" for p in svc2.proposals)
    assert svc2.proposals[0].native_draft is not None
    log = [e["entry"]["text"] for e in svc2.hub.kinds("engineer_log")]
    assert any(t.startswith("Backtested: NTP") for t in log) and any("saved run from" in t for t in log)
    assert first[0][0] == "Silence NTP flaps"


def test_load_saved_without_file(hist, tmp_path):
    svc = _service(FakeLLM([]), hist, tmp_path)

    async def go():
        with pytest.raises(EngineerRefused) as e:
            svc.load_saved()
        assert e.value.reason == "no_saved_run"
    asyncio.run(go())


def test_accept_reject_reset(hist, tmp_path):
    svc = _service(FakeLLM(GOOD_SCRIPT), hist, tmp_path)
    _run(svc)
    a, b = svc.proposals
    p, ar = svc.accept(a.id)
    assert p.status == "accepted" and p.active_rule_id == ar.id
    assert svc.activated[0][1] == "Silence NTP flaps" and svc.activated[0][2] == a.id
    with pytest.raises(ValueError):
        svc.accept(a.id)
    svc.reject(b.id)
    assert b.status == "rejected" and svc.state_payload()["pending"] == 0
    with pytest.raises(KeyError):
        svc.accept("p-nope")
    svc.reset()
    assert svc.state == "idle" and svc.proposals == [] and svc.log == []


def test_early_warnings_reach_triage_whatever_their_severity():
    from app.models import Event
    from app.triage.loop import triage_worthy
    base = dict(ts="2026-11-10T09:31:00Z", customer="nordhafen", source="signal_desk", vendor="Signal Desk rule",
                host="nhl-nas-01", service="storage", check="early_warning", native_severity="Warning", status="firing")
    warn = Event(id="w1", title="Early warning: Backup repository filling up", severity="warning",
                 produced_by="r-backup", **base)
    assert triage_worthy(warn)
    plain = Event(id="w2", title="NTP Time WARN", severity="warning", **{**base, "source": "checkmk", "check": "ntp_time"})
    assert not triage_worthy(plain)
    assert not triage_worthy(warn.model_copy(update={"status": "resolved"}))


# =============================================================== HTTP endpoints

def test_endpoints_load_saved_accept_reject_reset(tmp_path):
    from app import main
    saved = {"version": 1, "date": "2026-11-11", "saved_at": "2026-11-11T10:00:00Z",
             "log": [{"t": 1.0, "text": "Counted the alerts", "kind": "tool"}],
             "proposals": [
                 {"title": "Silence test VM agent alerts", "kind": "suppress", "reasoning": "Three test VMs.",
                  "rule": {"kind": "suppress", "match": {"source": "zabbix", "host_glob": "*-test-vm-*",
                                                         "title_contains": "agent is not available",
                                                         "severity_in": ["minor", "warning", "info"]},
                           "window_min": 1440, "max_per_window": 0},
                  "native_draft": {"dialect": "zabbix", "label": "Illustrative draft", "text": "x"}},
                 {"title": "Silence old SCOM hosts", "kind": "suppress", "reasoning": "Old hosts.",
                  "rule": {"kind": "suppress", "match": {"source": "scom", "host_glob": "*-win-old-*",
                                                         "title_contains": "Health Service unavailable",
                                                         "severity_in": ["warning", "info"]},
                           "window_min": 1440, "max_per_window": 0}, "native_draft": None},
             ]}
    path = tmp_path / "saved_run.json"
    path.write_text(json.dumps(saved))
    old_path = main.engineer.saved_path
    main.engineer.saved_path = path
    try:
        with TestClient(main.app) as c:
            c.post("/api/reset")
            assert c.post("/api/proposals/p-nope/accept").status_code == 404
            r = c.post("/api/engineer/load-saved")
            assert r.status_code == 202
            for _ in range(200):
                d = c.get("/api/proposals").json()
                if d["engineer"]["state"] == "done":
                    break
                time.sleep(0.05)
            assert d["engineer"]["state"] == "done" and d["engineer"]["replay_date"] == "2026-11-11"
            assert d["engineer"]["mode"] == "saved" and len(d["proposals"]) == 2
            p0, p1 = d["proposals"]
            assert p0["backtest"]["alerts_saved"] > 0 and p0["origin"] == "saved" and "rule_json" in p0
            assert "truth" not in json.dumps(d)
            acc = c.post(f"/api/proposals/{p0['id']}/accept")
            assert acc.status_code == 200 and acc.json()["proposal"]["status"] == "accepted"
            rules = c.get("/api/rules").json()["rules"]
            live = [r for r in rules if r.get("proposal_id") == p0["id"]]
            assert live and live[0]["enabled"] and live[0]["title"] == "Silence test VM agent alerts"
            assert c.post(f"/api/proposals/{p0['id']}/accept").status_code == 409
            rej = c.post(f"/api/proposals/{p1['id']}/reject")
            assert rej.status_code == 200 and rej.json()["proposal"]["status"] == "rejected"
            c.post("/api/reset")
            d2 = c.get("/api/proposals").json()
            assert d2["proposals"] == [] and d2["engineer"]["state"] == "idle"
            assert not any(r.get("proposal_id") for r in c.get("/api/rules").json()["rules"])
    finally:
        main.engineer.saved_path = old_path
