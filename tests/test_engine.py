"""Rule engine: validator rejections, the four kinds on hand-built streams, resolve pairing,
streaming == batch replay, and the backtest on a mini history with known truth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import HISTORY_PATH
from app.engine.backtest import RuleInvalid, backtest, backtest_tool, history_from_dicts
from app.engine.rules import Engine, Ev, describe, replay, validate
from app.models import Match, SuppressRule

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def mk(eid, minute, source="checkmk", title="NTP Time WARN on h1", host="h1", severity="warning",
       status="firing", customer="nordhafen", check="ntp_time", value=None, role="noise", incident=None):
    ts = (T0 + timedelta(seconds=round(minute * 60))).strftime("%Y-%m-%dT%H:%M:%SZ")
    if status == "resolved":
        title = f"Resolved: {title}"
    return {"id": eid, "ts": ts, "customer": customer, "source": source, "vendor": source.title(), "host": host,
            "service": "svc", "check": check, "title": title, "severity": severity, "native_severity": severity,
            "status": status, "value": value, "threshold": None, "raw": {},
            "truth": {"scenario_id": "s" if incident else None, "role": role, "incident_id": incident,
                      "pattern": None}}


def evs(rows):
    return [Ev.from_dict(r) for r in rows]


def run(rule_dict, rows, rid="r1"):
    rule, errs = validate(rule_dict)
    assert not errs, errs
    return [e.id for e in replay(evs(rows), [(rid, "Rule", rule)])]


# ================================================================ validator

SUPPRESS_OK = {"kind": "suppress", "match": {"source": "checkmk", "title_contains": "NTP Time", "severity_in": ["warning"]},
               "window_min": 60, "max_per_window": 0}


def _errs(d):
    rule, errs = validate(d)
    assert rule is None and errs
    return " | ".join(errs)


def test_validator_accepts_good_rules():
    for d in (
        SUPPRESS_OK,
        {"kind": "suppress", "match": {"source": "veeam", "title_contains": "Success"}, "window_min": 1, "max_per_window": 0},
        {"kind": "precursor", "match": {"source": "netapp", "title_contains": "Nearly Full"}, "count": 2,
         "within_min": 720, "raise": {"title": "Backup volume filling", "severity": "major"}},
        {"kind": "retune", "source": "zabbix", "check": "cpu_util", "op": ">", "new_threshold": 90, "sustain_min": 5},
        {"kind": "correlate", "members": [{"source": "fortigate", "title_contains": "VPN"},
                                          {"source": "prtg", "title_contains": "Ping Down"}], "within_min": 1440},
    ):
        rule, errs = validate(d)
        assert rule is not None and errs == [], (d, errs)
        assert describe(rule)


def test_validator_unknown_keys():
    assert "unknown key 'foo'" in _errs({**SUPPRESS_OK, "foo": 1})
    e = _errs({**SUPPRESS_OK, "match": {"source": "checkmk", "hostname": "x", "severity_in": ["warning"]}})
    assert "unknown key 'hostname' in match" in e and "host_glob" in e


def test_validator_unknown_kind_source_customer_check():
    assert "kind must be one of" in _errs({"kind": "mute", "match": {"source": "checkmk"}})
    assert "not a known source" in _errs({**SUPPRESS_OK, "match": {"source": "nagios", "severity_in": ["warning"]}})
    assert "not a known customer" in _errs({**SUPPRESS_OK, "match": {"source": "checkmk", "customer": "acme",
                                                                     "severity_in": ["warning"]}})
    assert "not a check of zabbix" in _errs({"kind": "retune", "source": "zabbix", "check": "cpu", "op": ">",
                                             "new_threshold": 90})
    assert "no numeric value" in _errs({"kind": "retune", "source": "zabbix", "check": "agent_unavailable",
                                        "op": ">", "new_threshold": 1})
    assert "not a known source" in _errs({"kind": "retune", "source": "nagios", "check": "cpu", "op": ">",
                                          "new_threshold": 1})


def test_validator_rejects_match_all():
    assert "matches too much" in _errs({**SUPPRESS_OK, "match": {}})
    assert "matches too much" in _errs({**SUPPRESS_OK, "match": {"severity_in": ["warning", "info"]}})
    assert "matches too much" in _errs({**SUPPRESS_OK, "match": {"customer": "nordhafen", "severity_in": ["info"]}})
    assert "matches every host" in _errs({**SUPPRESS_OK, "match": {"host_glob": "*", "severity_in": ["info"]}})
    assert "at least 3 characters" in _errs({**SUPPRESS_OK, "match": {"title_contains": "a", "severity_in": ["info"]}})
    assert "members[1] matches too much" in _errs(
        {"kind": "correlate", "members": [{"source": "prtg"}, {"customer": "nordhafen"}], "within_min": 5})


def test_validator_window_bounds():
    for w in (0, 1441, -5):
        assert "between 1 and 1440" in _errs({**SUPPRESS_OK, "window_min": w})
    assert "between 1 and 1440" in _errs({"kind": "precursor", "match": {"source": "netapp"}, "count": 2,
                                          "within_min": 2000, "raise": {"title": "abc", "severity": "major"}})
    assert "between 1 and 1440" in _errs({"kind": "correlate", "members": [{"source": "prtg"}, {"source": "scom"}],
                                          "within_min": 0})


def test_validator_suppress_never_touches_critical():
    # Zabbix has "Disaster" -> critical: without severity_in this could drop critical alerts
    assert "critical" in _errs({"kind": "suppress", "match": {"source": "zabbix"}, "window_min": 60, "max_per_window": 0})
    assert "critical" in _errs({"kind": "suppress", "match": {"source": "checkmk", "severity_in": ["critical", "major"]},
                                "window_min": 60, "max_per_window": 0})
    assert "critical" in _errs({"kind": "suppress", "match": {"title_contains": "down"}, "window_min": 60,
                                "max_per_window": 0})
    # Veeam never emits critical
    assert validate({"kind": "suppress", "match": {"source": "veeam"}, "window_min": 60, "max_per_window": 0})[0]


def test_validator_misc():
    assert "missing key 'match'" in _errs({"kind": "suppress", "window_min": 60, "max_per_window": 0})
    assert "keep must be an index" in _errs({"kind": "correlate", "members": [{"source": "prtg"}, {"source": "scom"}],
                                             "within_min": 5, "keep": 3})
    assert "2 to 4" in _errs({"kind": "correlate", "members": [{"source": "prtg"}], "within_min": 5})
    assert "valid JSON" in _errs("{nope")


# ================================================================ evaluator: suppress

def test_suppress_all_drops_matches_and_their_resolves():
    rows = [mk("a", 0), mk("a-r", 2, status="resolved"), mk("b", 3, source="zabbix", title="CPU", check="cpu"),
            mk("c", 4, title="NTP Time WARN on h1", severity="critical")]
    out = run(SUPPRESS_OK, rows)
    assert out == ["b", "c"]          # c is critical: does not match severity_in


def test_suppress_engine_guard_never_drops_critical():
    rule = SuppressRule(match=Match(source="checkmk"), window_min=60, max_per_window=0)   # bypasses validate
    out = replay(evs([mk("a", 0, severity="critical"), mk("b", 1)]), [("r", "R", rule)])
    assert [e.id for e in out] == ["a"]


def test_suppress_per_host_window():
    rule = {**SUPPRESS_OK, "window_min": 10, "max_per_window": 1}
    rows = [mk("a1", 0), mk("b1", 1, host="h2"), mk("a2", 3), mk("a3", 9.9), mk("a4", 10.5), mk("b2", 4, host="h2")]
    rows.sort(key=lambda r: r["ts"])
    assert run(rule, rows) == ["a1", "b1", "a4"]


def test_resolve_pairs_fifo_with_its_firing():
    rule = {**SUPPRESS_OK, "window_min": 10, "max_per_window": 1}
    rows = [mk("f1", 0), mk("f2", 1), mk("r1", 2, status="resolved"), mk("r2", 3, status="resolved")]
    assert run(rule, rows) == ["f1", "r1"]          # f2 dropped, so its resolve r2 is dropped too


# ================================================================ evaluator: retune

RETUNE = {"kind": "retune", "source": "zabbix", "check": "cpu_util", "op": ">", "new_threshold": 90, "sustain_min": 5}


def cpu(eid, minute, value, status="firing", host="h1"):
    return mk(eid, minute, source="zabbix", check="cpu_util", title="High CPU utilization", value=value,
              status=status, host=host, severity="minor")


def test_retune_threshold_and_sustain():
    rows = [
        cpu("low", 0, 85), cpu("low-r", 1, 85, "resolved"),             # below the new threshold
        cpu("blip", 2, 95, host="h2"), cpu("blip-r", 4, 95, "resolved", host="h2"),   # cleared after 2 min
        cpu("real", 10, 97, host="h3"), cpu("x", 12, None, host="h9"),
        cpu("real-r", 40, 97, "resolved", host="h3"),
    ]
    assert run(RETUNE, rows) == ["x", "real", "real-r"]   # real released 5 min after it fired


def test_retune_without_sustain_keeps_breaches_immediately():
    rule = {**RETUNE, "sustain_min": 0}
    assert run(rule, [cpu("a", 0, 85), cpu("b", 1, 95, host="h2")]) == ["b"]


# ================================================================ evaluator: correlate

CORR = {"kind": "correlate", "members": [{"source": "fortigate", "title_contains": "VPN"},
                                         {"source": "prtg", "title_contains": "Ping Down"}],
        "within_min": 5, "keep": 1}


def test_correlate_groups_members_and_keeps_children():
    rows = [mk("vpn", 0, source="fortigate", title="IPsec VPN tunnel down", host="fw1", check="vpn_down"),
            mk("ping", 1, source="prtg", title="Ping Down on br1", host="br1", check="ping", severity="critical"),
            mk("vpn-r", 3, source="fortigate", title="IPsec VPN tunnel down", host="fw1", check="vpn_down",
               status="resolved"),
            mk("other", 2, customer="valdorina", source="prtg", title="Ping Down on br9", host="br9", check="ping"),
            mk("late", 30, source="prtg", title="Ping Down on br2", host="br2", check="ping")]
    rows.sort(key=lambda r: r["ts"])
    rule, _ = validate(CORR)
    out = replay(evs(rows), [("g", "Branch blip", rule)])
    grouped = [e for e in out if e.kind == "group"]
    assert len(grouped) == 1
    g = grouped[0]
    assert g.children == ("vpn", "ping") and g.produced_by == "g"
    assert g.host == "br1" and g.severity == "critical"           # keep=1: the PRTG alert heads the group
    ids = [e.id for e in out]
    assert "vpn" not in ids and "ping" not in ids and "vpn-r" not in ids
    assert "other" in ids and "late" in ids                       # other customer / lone member pass unchanged


# ================================================================ evaluator: precursor

PRE = {"kind": "precursor", "match": {"source": "redgate", "title_contains": "Long-running"}, "count": 2,
       "within_min": 10, "raise": {"title": "DB pool at risk", "severity": "major"}}


def lrq(eid, minute):
    return mk(eid, minute, source="redgate", title="Long-running query on sql1", host="sql1",
              check="long_running_query", severity="minor")


def test_precursor_raises_once_and_drops_nothing():
    rows = [lrq("q1", 0), lrq("q2", 5), lrq("q3", 7), lrq("q4", 30), lrq("q5", 45), lrq("q6", 48)]
    rule, _ = validate(PRE)
    out = replay(evs(rows), [("p", "Pool", rule)])
    warn = [e for e in out if e.kind == "warning"]
    assert [w.children for w in warn] == [("q1", "q2"), ("q5", "q6")]   # q3 inside quiet time, q4 alone
    assert all(w.produced_by == "p" and w.severity == "major" for w in warn)
    assert [e.id for e in out if e.kind == "event"] == ["q1", "q2", "q3", "q4", "q5", "q6"]


# ================================================================ streaming == batch

def _mixed_rows():
    rows = []
    for i in range(60):
        rows.append(mk(f"n{i}", i * 0.7, host=f"h{i % 3}"))
        if i % 4 == 0:
            rows.append(mk(f"n{i}-r", i * 0.7 + 0.5, host=f"h{i % 3}", status="resolved"))
        if i % 5 == 0:
            rows.append(cpu(f"c{i}", i * 0.7 + 0.1, 80 + i % 20, host=f"h{i % 2}"))
            rows.append(cpu(f"c{i}-r", i * 0.7 + 1 + i % 7, 80 + i % 20, "resolved", host=f"h{i % 2}"))
        if i % 9 == 0:
            rows.append(mk(f"v{i}", i * 0.7 + 0.2, source="fortigate", title="IPsec VPN tunnel down", host="fw",
                           check="vpn_down"))
            rows.append(mk(f"p{i}", i * 0.7 + 0.3, source="prtg", title="Ping Down on br", host="br", check="ping"))
        if i % 6 == 0:
            rows.append(lrq(f"q{i}", i * 0.7 + 0.4))
    rows.sort(key=lambda r: r["ts"])
    return rows


def _all_rules():
    specs = []
    for rid, d in (("s", {**SUPPRESS_OK, "window_min": 3, "max_per_window": 1}), ("t", RETUNE), ("g", CORR), ("p", PRE)):
        rule, errs = validate(d)
        assert not errs
        specs.append((rid, rid.upper(), rule))
    return specs


def test_streaming_equals_batch_replay():
    rows = _mixed_rows()
    batch = [e.id for e in replay(evs(rows), _all_rules())]
    eng = Engine(_all_rules())
    streamed = []
    for ev in evs(rows):                       # one at a time, like the live stream
        streamed.extend(e.id for e in eng.process(ev))
    streamed.extend(e.id for e in eng.flush())
    assert streamed == batch
    assert batch == [e.id for e in replay(evs(rows), _all_rules())]     # deterministic
    kinds = {e.kind for e in replay(evs(rows), _all_rules())}
    assert {"event", "group", "warning"} <= kinds
    assert len(batch) < len(rows)


# ================================================================ backtest: mini history with known truth

def mini_history():
    rows = []
    for i in range(10):                                          # NTP noise on two hosts
        rows.append(mk(f"ntp{i}", i * 10, host=f"h{i % 2}"))
    rows += [
        lrq("pre1", 60), lrq("pre2", 70),                            # precursors of incident I1
        mk("root1", 100, source="redgate", title="Blocking process on sql1", host="sql1", check="blocking",
           severity="major", role="root", incident="I1"),
        mk("sym1", 101, source="zabbix", title="PostgreSQL: Too many connections", host="db1",
           check="pg_connections", severity="major", role="symptom", incident="I1"),
        lrq("orph1", 500), lrq("orph2", 505),                        # orphan run: leads nothing
        mk("root2", 600, source="checkmk", title="Host web1 DOWN", host="web1", check="host_down",
           severity="critical", role="root", incident="I2"),
    ]
    for r in rows:
        if r["id"] in ("pre1", "pre2"):
            r["truth"].update(role="precursor", incident_id="I1", scenario_id="s")
    rows.sort(key=lambda r: r["ts"])
    return history_from_dicts(rows)


def test_backtest_suppress_exact_numbers():
    h = mini_history()
    bt = backtest(SUPPRESS_OK, [], history=h)
    assert (bt.alerts_before, bt.alerts_after, bt.alerts_saved, bt.saved_pct) == (17, 7, 10, 58.8)
    assert (bt.incidents_total, bt.incidents_visible, bt.incidents_hidden) == (2, 2, 0)
    assert len(bt.samples) == 5 and all("NTP Time" in s for s in bt.samples)
    assert bt.precursor_fired == 0 and bt.median_lead_min is None


def test_backtest_reports_hidden_incident():
    h = mini_history()
    active = [{"kind": "suppress", "match": {"source": "zabbix", "title_contains": "Too many", "severity_in": ["major"]},
               "window_min": 60, "max_per_window": 0}]
    cand = {"kind": "suppress", "match": {"source": "redgate", "severity_in": ["major"]}, "window_min": 60,
            "max_per_window": 0}
    bt = backtest(cand, active, history=h)
    assert (bt.alerts_before, bt.alerts_after, bt.alerts_saved) == (16, 15, 1)
    assert (bt.incidents_total, bt.incidents_visible, bt.incidents_hidden) == (2, 1, 1)
    # the same candidate alone leaves the symptom visible
    assert backtest(cand, [], history=h).incidents_hidden == 0


def test_backtest_precursor_true_and_false():
    h = mini_history()
    bt = backtest({**PRE, "within_min": 15}, [], history=h)
    assert (bt.precursor_fired, bt.precursor_true, bt.precursor_false) == (2, 1, 1)
    assert bt.median_lead_min == 30.0                       # warning at 70, root at 100
    assert (bt.alerts_before, bt.alerts_after, bt.alerts_saved) == (17, 19, -2)
    assert bt.incidents_hidden == 0 and all(s.startswith("raised:") for s in bt.samples)


CERT_PRE = {"kind": "precursor", "match": {"source": "checkmk", "title_contains": "Certificate expires"}, "count": 1,
            "within_min": 1440, "raise": {"title": "Portal certificate about to expire", "severity": "major"}}


def cert_history():
    """A certificate warning 30 h before an expiry (incident C1), and an unrelated one that leads
    nowhere (an orphan, then a different incident C2 at the same customer 20 h later)."""
    H = 60
    rows = [
        mk("cw1", 0, title="Certificate expires in 2 days on web1", host="web1", check="https_cert",
           role="precursor", incident="C1"),
        mk("croot", 30 * H, title="Certificate expired on web1", host="web1", check="https_cert_expired",
           severity="critical", role="root", incident="C1"),
        mk("cw2", 100 * H, title="Certificate expires in 2 days on web2", host="web2", check="https_cert"),
        mk("root2", 120 * H, source="redgate", title="Blocking process on sql1", host="sql1", check="blocking",
           severity="major", role="root", incident="C2"),
    ]
    return history_from_dicts(rows)


def test_precursor_credit_window_follows_the_rule_horizon():
    h = cert_history()
    short = backtest(CERT_PRE, [], history=h)                      # default 6 h: the 30 h warning is "false"
    assert (short.precursor_fired, short.precursor_true, short.precursor_false) == (2, 0, 2)
    long = backtest({**CERT_PRE, "horizon_h": 48}, [], history=h)
    assert (long.precursor_fired, long.precursor_true, long.precursor_false) == (2, 1, 1)
    assert long.median_lead_min == 30 * 60.0
    # even the longest horizon never credits a warning whose alerts are not part of the incident
    # (cw2 is followed by incident C2 at the same customer 20 h later): false-alarm counting holds
    widest = backtest({**CERT_PRE, "horizon_h": 72}, [], history=h)
    assert (widest.precursor_true, widest.precursor_false) == (1, 1)


def test_horizon_is_validated_and_described():
    assert validate({**CERT_PRE, "horizon_h": 73})[1] == ["horizon_h must be a whole number of hours between 1 and 72"]
    assert validate({**CERT_PRE, "horizon_h": 0})[1]
    rule, errs = validate({**CERT_PRE, "horizon_h": 48})
    assert not errs and "Expects the failure within 48 hours." in describe(rule)
    assert "Expects" not in describe(validate(CERT_PRE)[0])         # default: unchanged wording


def test_backtest_correlate_counts_group_as_one_and_keeps_incident():
    rows = [mk("a", 0, source="prtg", title="Ping Down on br1", host="br1", check="ping", severity="critical",
               role="symptom", incident="I9"),
            mk("b", 1, source="fortigate", title="IPsec VPN tunnel down", host="fw", check="vpn_down"),
            mk("root", 0.5, source="solarwinds", title="Interface Te1 down", host="core", check="iface_down",
               role="root", incident="I9", severity="critical")]
    rows.sort(key=lambda r: r["ts"])
    rule = {**CORR, "members": CORR["members"] + [{"source": "solarwinds", "title_contains": "Interface"}]}
    bt = backtest(rule, [], history=history_from_dicts(rows))
    assert (bt.alerts_before, bt.alerts_after, bt.alerts_saved) == (3, 1, 2)
    assert (bt.incidents_visible, bt.incidents_hidden) == (1, 0)       # children of the group stay visible


def test_backtest_invalid_rule():
    with pytest.raises(RuleInvalid) as e:
        backtest({"kind": "suppress", "match": {}, "window_min": 5, "max_per_window": 0}, [], history=mini_history())
    assert any("matches too much" in x for x in e.value.errors)
    r = backtest_tool({"kind": "nope"}, [])
    assert r["ok"] is False and r["errors"]


# ================================================================ real history

needs_history = pytest.mark.skipif(not HISTORY_PATH.exists(), reason="data/history.jsonl not built")


@needs_history
def test_real_history_incident_hiding_rule_is_flagged():
    # with SAP and the ServiceNow ticket already muted, muting Control-M hides the job-chain incidents
    active = [
        {"kind": "suppress", "match": {"source": "sap_alm", "title_contains": "Job failed", "severity_in": ["major"]},
         "window_min": 60, "max_per_window": 0},
        {"kind": "suppress", "match": {"source": "servicenow", "title_contains": "finance extract",
                                       "severity_in": ["minor"]}, "window_min": 60, "max_per_window": 0},
    ]
    cand = {"kind": "suppress", "match": {"source": "controlm", "title_contains": "Job ", "severity_in": ["major", "info"]},
            "window_min": 60, "max_per_window": 0}
    bt = backtest(cand, active)
    assert bt.incidents_hidden > 0
    assert bt.incidents_visible == bt.incidents_total - bt.incidents_hidden


@needs_history
def test_real_history_certificate_warning_can_be_proposed():
    """Certificate expiry warnings come 1-2 days ahead; with a matching horizon the engineer's
    server-side check keeps such a rule (before, the 6 h window scored every warning false)."""
    from app.engine.store import seed_rules
    from app.engineer.agent import drop_reason
    rule = {"kind": "precursor", "match": {"source": "alertmanager", "title_contains": "TLS certificate"},
            "count": 1, "within_min": 1440, "raise": {"title": "Portal certificate about to expire",
                                                      "severity": "major"}}
    six = backtest(rule, seed_rules())
    assert six.precursor_fired > 0 and six.precursor_true == 0
    r48, _ = validate({**rule, "horizon_h": 48})
    bt = backtest(r48, seed_rules())
    assert bt.precursor_true >= 3 and bt.incidents_hidden == 0
    assert drop_reason(r48, bt) is None, (bt.precursor_true, bt.precursor_fired, bt.median_lead_min)


@needs_history
def test_real_history_backtest_is_fast_and_safe():
    import time
    from app.engine.store import seed_rules
    rule = {"kind": "suppress", "match": {"source": "checkmk", "title_contains": "NTP Time WARN", "severity_in": ["warning"]},
            "window_min": 1440, "max_per_window": 1}
    backtest(rule, seed_rules())                  # warm: history load + baseline
    t = time.time()
    bt = backtest(rule, seed_rules())
    assert time.time() - t < 2.0
    assert bt.alerts_saved > 1000 and bt.incidents_hidden == 0
    assert "truth" not in bt.model_dump_json()
