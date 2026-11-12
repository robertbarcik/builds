"""Triage loop with a fake LLM (no network): merge rules, aging, truth never in prompts, degraded
mode and probing, idle and quiet ticks, the dismiss endpoint."""
from __future__ import annotations

import asyncio
import json
from fastapi.testclient import TestClient

from app.config import load_config
from app.llm.budget import Budget, BudgetRefused
from app.llm.client import LLMError, Usage
from app.models import Event, Truth
from app.prompts.triage import PromptLeak, assert_prompt_clean, triage_messages
from app.sim.catalog import CUSTOMERS, iso
from app.sse import Hub
from app.triage.loop import TriageLoop, attach_entries, dealias, drop_repeated_cause, family_of, repeats

CFG = load_config()
T0 = 1_762_767_000.0                       # 2026-11-10T09:30:00Z


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def now(self):
        return self.t


class FakeSim:
    def __init__(self):
        self.clock = FakeClock()
        self.epoch = 1
        self.listeners = []


class Real:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeStream:
    def __init__(self, text):
        self._text = text
        self.text = ""
        self.usage = Usage(10, 10, 0.00001, "fake")

    def __aiter__(self):
        async def gen():
            for w in self._text.split(" "):
                self.text += w + " "
                yield w + " "
            self.text = self.text.strip()
        return gen()


class FakeLLM:
    """Scripted json_call results (dicts, or exceptions to raise) and a fixed summary."""

    def __init__(self, results=None, summary="The core uplink failed. Everything behind it is unreachable."):
        self.results = list(results or [])
        self.summary = summary
        self.json_msgs = []
        self.stream_msgs = []

    async def json_call(self, role, messages, **kw):
        self.json_msgs.append(messages)
        r = self.results.pop(0) if self.results else {"attach": [], "new": [], "noise_ids": []}
        if isinstance(r, Exception):
            raise r
        return r, Usage(100, 20, 0.0001, "fake")

    def stream_text(self, role, messages, **kw):
        self.stream_msgs.append(messages)
        return FakeStream(self.summary)


def ev(i, *, sec=0, customer="nordhafen", source="checkmk", host="nhl-win-01", severity="major",
       title="Host nhl-win-01 UNREACH", status="firing", check="host_unreach", secret=True):
    return Event(id=f"E{i}", ts=iso(T0 + sec), customer=customer, source=source, vendor=source.title(), host=host,
                 service="svc", check=check, title=title, severity=severity, native_severity="X", status=status,
                 truth=Truth(scenario_id="core_uplink_down", role="symptom", incident_id="SECRET#1",
                             pattern="scenario:core_uplink_down") if secret else None)


def loop_with(llm, clients=1, cfg=CFG):
    hub = Hub()
    for _ in range(clients):
        hub.subscribe()
    real = Real()
    sim = FakeSim()
    tl = TriageLoop(sim, hub, llm, Budget(cfg, time_fn=real), cfg, time_fn=real)
    return tl, sim, hub, real


def feed(tl, events):
    for fn in tl.sim.listeners:
        fn(events)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- merge rules

def test_unknown_ids_dropped_attach_vs_new():
    tl, sim, hub, _ = loop_with(FakeLLM())
    batch = [ev(1), ev(2, host="nhl-win-02", title="Host nhl-win-02 UNREACH"),
             ev(3, source="solarwinds", host="nhl-core-sw-01", title="Interface Te1/1/1 on nhl-core-sw-01 is down",
                severity="critical", check="iface_down")]
    r = tl.apply_result({"new": [{"customer": "nordhafen", "title": "Core uplink down", "severity": "critical",
                                  "likely_cause": "Te1/1/1 down", "next_step": "Check Te1/1/1",
                                  "event_ids": ["E3", "E1", "NOPE"]}],
                         "attach": [{"incident_id": "INC-404", "event_ids": ["E2"]}],
                         "noise_ids": ["E9"]}, batch)
    assert r["opened"] == 1 and r["unknown_event_ids"] == 1 and r["unknown_incidents"] == 1
    (card,) = tl.cards.values()
    assert card.evidence_total == 2 and card.inc.severity == "critical" and card.inc.origin == "llm"
    # next tick: attach to the existing card; an event already used cannot be reused
    batch2 = [ev(4, sec=60, host="nhl-win-03", title="Host nhl-win-03 UNREACH")]
    r2 = tl.apply_result({"attach": [{"incident_id": card.id, "event_ids": ["E4", "E4"]}],
                          "new": [{"customer": "nordhafen", "title": "Other", "event_ids": ["E4"]}]}, batch2)
    assert r2["attached"] == 1 and r2.get("opened", 0) == 0 and r2["new_dropped"] == 1
    assert card.evidence_total == 3 and len(tl.cards) == 1


def test_aliases_map_back_and_unknown_refs_stay_unknown():
    out = dealias({"attach": [{"incident_id": "INC-1", "event_ids": ["e1", "e9"]}],
                   "new": [{"title": "x", "event_ids": ["e2"]}], "noise_ids": ["e3"]},
                  {"e1": "L1-000001", "e2": "L1-000002", "e3": "L1-000003"})
    assert out["attach"][0]["event_ids"] == ["L1-000001", "e9"]
    assert out["new"][0]["event_ids"] == ["L1-000002"] and out["noise_ids"] == ["L1-000003"]
    assert dealias("garbage", {}) == "garbage"


def test_attach_shapes_and_threshold_guard():
    assert attach_entries([["INC-1", ["e1"]], {"incident_id": "INC-2", "event_ids": ["e2"]}, "junk"]) == [
        {"incident_id": "INC-1", "event_ids": ["e1"]}, {"incident_id": "INC-2", "event_ids": ["e2"]}]
    assert attach_entries({"INC-3": ["e3"]}) == [{"incident_id": "INC-3", "event_ids": ["e3"]}]
    tl, *_ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Branch router down",
                              "event_ids": ["E1"]}]}, [ev(1, host="nhl-br-kiel-rt01")])
    cid = next(iter(tl.cards))
    cpu_other = ev(2, host="nhl-app-01", severity="minor", title="High CPU utilization (over 80% for 1m)")
    cpu_same = ev(3, host="nhl-br-kiel-rt01", severity="minor", title="High CPU utilization (over 80% for 1m)")
    r = tl.apply_result({"attach": [[cid, ["E2", "E3"]]]}, [cpu_other, cpu_same])
    assert r["attached"] == 1 and r["attach_rejected"] == 1
    card = tl.cards[cid]
    assert card.inc.severity == "major"
    tl.apply_result({"attach": [{"incident_id": cid, "event_ids": ["E4"], "severity": "critical"}]},
                    [ev(4, host="nhl-acc-sw-01", title="Node nhl-acc-sw-01 is down")])
    assert card.inc.severity == "critical"
    tl.apply_result({"attach": [{"incident_id": cid, "event_ids": ["E5"], "severity": "minor"}]},
                    [ev(5, host="nhl-acc-sw-02", title="Node nhl-acc-sw-02 is down")])
    assert card.inc.severity == "critical"                     # never lowered


def test_new_incident_duplicate_title_is_merged_and_bad_fields_fixed():
    tl, *_ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Core uplink down at Nordhafen",
                              "severity": "critical", "event_ids": ["E1"]}]}, [ev(1)])
    r = tl.apply_result({"new": [{"customer": "atlantis", "title": "Core uplink down at Nordhafen",
                                  "severity": "catastrophic", "event_ids": ["E2"]}]}, [ev(2, sec=30)])
    assert r["merged"] == 1 and len(tl.cards) == 1
    assert next(iter(tl.cards.values())).evidence_total == 2
    r3 = tl.apply_result({"new": [{"customer": "zzz", "title": "Disk", "severity": "catastrophic",
                                   "event_ids": ["E5"]}]},
                         [ev(5, customer="valdorina", host="bva-app-01", title="Low disk", severity="minor")])
    c = [c for c in tl.cards.values() if c.inc.customer == "valdorina"][0]
    assert r3["opened"] == 1 and c.inc.severity == "minor"


def test_attach_refuses_other_customers_events():
    tl, *_ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Uplink", "event_ids": ["E1"]}]}, [ev(1)])
    cid = next(iter(tl.cards))
    r = tl.apply_result({"attach": [{"incident_id": cid, "event_ids": ["E2"]}]},
                        [ev(2, customer="valdorina", host="bva-win-01")])
    assert r.get("attached", 0) == 0


def test_quiet_then_expiry_then_reopen():
    tl, sim, hub, _ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Uplink", "severity": "major",
                              "event_ids": ["E1"]}]}, [ev(1)])
    card = next(iter(tl.cards.values()))
    tl._age(T0 + 10 * 60)
    assert card.inc.status == "open"                         # still firing: an outage does not fade
    tl._age(T0 + 30 * 60)
    assert card.inc.status == "quiet" and tl.open_count() == 0 and len(tl.board()) == 1
    tl._add_evidence(card, [ev(2, sec=31 * 60, host="nhl-win-02")])   # new evidence wakes it up
    assert card.inc.status == "open" and tl.open_count() == 1
    feed(tl, [ev(3, sec=32 * 60, status="resolved"), ev(4, sec=33 * 60, host="nhl-win-02", status="resolved")])
    tl._select(T0 + 34 * 60)
    tl._age(T0 + 41 * 60)                                    # 10 min quiet and resolved -> quiet
    assert card.inc.status == "quiet" and tl.board()[0]["recovering"]
    tl._age(T0 + 41 * 60 + 45 * 60 + 2)
    assert tl.board() == [] and tl.cards == {}


def test_resolve_seen_before_the_card_still_counts():
    tl, sim, hub, _ = loop_with(FakeLLM())
    fire = ev(1, host="bva-br-verona-rt01", title="Host bva-br-verona-rt01 UNREACH")
    other = ev(3, sec=30, source="prtg", check="ping", host="bva-br-verona-rt01", title="Ping Down")
    res = ev(2, sec=240, host="bva-br-verona-rt01", title="Resolved: Host bva-br-verona-rt01 UNREACH",
             status="resolved")
    feed(tl, [fire, other, res])
    batch, _ = tl._select(T0 + 300)                        # the resolve is noted before any card exists
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Branch router down",
                              "event_ids": ["E1", "E3"]}]}, batch)
    card = next(iter(tl.cards.values()))
    assert ("checkmk", "bva-br-verona-rt01", "host_unreach") in card.resolved
    tl._age(T0 + 11 * 60)
    (c,) = tl.board()
    assert c["status"] == "quiet" and not c["recovering"]           # half cleared: quiet, not recovering


def test_blip_that_already_cleared_opens_no_card():
    """A branch router back within minutes: the model may call it an incident, the board does not."""
    tl, sim, hub, _ = loop_with(FakeLLM())
    fire = ev(1, source="prtg", check="ping", host="nhl-br-kiel-rt01", title="Ping Down", severity="critical")
    res = ev(2, sec=240, source="prtg", check="ping", host="nhl-br-kiel-rt01", title="Ping Up", status="resolved")
    feed(tl, [fire, res])
    batch, _ = tl._select(T0 + 300)
    r = tl.apply_result({"new": [{"customer": "nordhafen", "title": "Branch router nhl-br-kiel-rt01 down",
                                  "severity": "major", "event_ids": ["E1"]}]}, batch)
    assert r["new_recovered"] == 1 and tl.board() == [] and tl.open_count() == 0


def test_recovered_card_leaves_the_headline_count_quickly():
    tl, sim, hub, _ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Branch router down", "severity": "major",
                              "event_ids": ["E1", "E2"]}]},
                    [ev(1, source="prtg", check="ping", host="nhl-br-kiel-rt01", title="Ping Down"),
                     ev(2, source="solarwinds", check="node_down", host="nhl-br-kiel-rt01", title="Node down")])
    assert tl.open_count() == 1
    feed(tl, [ev(3, sec=200, source="prtg", check="ping", host="nhl-br-kiel-rt01", title="Ping Up",
                 status="resolved")])
    tl._select(T0 + 240)
    tl._age(T0 + 6 * 60)
    assert tl.open_count() == 1                                      # one of two still down: stays
    feed(tl, [ev(4, sec=300, source="solarwinds", check="node_down", host="nhl-br-kiel-rt01", title="Node up",
                 status="resolved")])
    tl._select(T0 + 320)
    tl._age(T0 + 6 * 60)                                             # all cleared, 6 min idle (>= 3)
    (c,) = tl.board()
    assert tl.open_count() == 0 and c["status"] == "quiet" and c["recovering"]
    # the UI's templated status line: cleared at the LATEST resolve on the card
    assert c["cleared_ts"] == iso(T0 + 300)


def test_open_and_quiet_unrecovered_cards_have_no_cleared_line():
    tl, sim, hub, _ = loop_with(FakeLLM())
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Uplink", "severity": "major",
                              "event_ids": ["E1"]}]}, [ev(1)])
    (c,) = tl.board()
    assert c["cleared_ts"] is None and not c["recovering"]
    tl._age(T0 + 31 * 60)                                            # quiet, still firing
    (c,) = tl.board()
    assert c["status"] == "quiet" and not c["recovering"] and c["cleared_ts"] is None


def test_board_cap_minor_cannot_displace():
    cfg = json.loads(json.dumps(CFG))
    cfg["triage"]["max_cards"] = 2
    cfg["triage"]["max_new_per_tick"] = 5
    tl, *_ = loop_with(FakeLLM(), cfg=cfg)
    batch = [ev(i, host=f"h{i}", title=f"distinct thing number {i} zz{i}") for i in range(1, 5)]
    tl.apply_result({"new": [{"customer": "nordhafen", "title": f"Problem alpha{i} beta{i}", "severity": "minor",
                              "event_ids": [f"E{i}"]} for i in (1, 2, 3)]}, batch[:3])
    assert len(tl.board()) == 2
    tl.apply_result({"new": [{"customer": "nordhafen", "title": "Big outage gamma", "severity": "critical",
                              "event_ids": ["E4"]}]}, batch[3:])
    titles = [c["title"] for c in tl.board()]
    assert len(titles) == 2 and "Big outage gamma" in titles


# ---------------------------------------------------------------- truth never reaches the model

def test_truth_absent_from_prompts():
    llm = FakeLLM([{"new": [{"customer": "nordhafen", "title": "Core uplink down", "severity": "critical",
                             "likely_cause": "c", "next_step": "n", "event_ids": ["e1", "e2"]}]}])
    tl, *_ = loop_with(llm)
    feed(tl, [ev(1), ev(2, sec=5, host="nhl-win-02")])
    out = run(tl.tick())
    assert out.get("opened") == 1 and llm.json_msgs and llm.stream_msgs
    for msgs in llm.json_msgs + llm.stream_msgs:
        blob = json.dumps(msgs)
        for secret in ("truth", "SECRET#1", "scenario:core_uplink_down", "core_uplink_down"):
            assert secret not in blob, secret


def test_prompt_guard_catches_a_leak():
    try:
        assert_prompt_clean([{"role": "user", "content": "x pattern noise:checkmk_ntp"}])
    except PromptLeak:
        pass
    else:
        raise AssertionError("leak not caught")
    msgs = triage_messages(["E1|09:30:00|nordhafen|checkmk|h|major|t"], [], CUSTOMERS, "09:30:00")
    assert "E1|09:30:00" in msgs[1]["content"]


# ---------------------------------------------------------------- summaries stream as deltas

def test_summary_streams_deltas_then_upsert():
    llm = FakeLLM([{"new": [{"customer": "nordhafen", "title": "Core uplink down", "severity": "critical",
                             "event_ids": ["e1"]}]}])
    tl, sim, hub, _ = loop_with(llm)
    q = next(iter(hub._clients))
    feed(tl, [ev(1)])
    run(tl.tick())
    types = []
    while not q.empty():
        types.append(json.loads(q.get_nowait()[6:])["type"])
    assert "incident_upsert" in types and types.count("incident_delta") >= 3
    card = next(iter(tl.cards.values()))
    assert card.inc.summary.startswith("The core uplink failed") and card.summary_state == "done"


def test_summary_does_not_repeat_the_likely_cause():
    cause = "Interface Te1/1/1 on nhl-core-sw-01 went down, cutting off everything behind it."
    s = ("Interface Te1/1/1 on nhl-core-sw-01 went down and cut off everything behind it. "
         "Nordhafen's branch sites, Windows servers and the customer portal are unreachable and still down.")
    out = drop_repeated_cause(s, cause)
    assert out.startswith("Nordhafen's branch sites") and "went down" not in out
    assert drop_repeated_cause("Branch offices cannot reach the ERP.", cause) == "Branch offices cannot reach the ERP."
    assert drop_repeated_cause(cause, cause) == cause                  # never left empty
    assert not repeats("The portal is down for customers.", cause)
    # paraphrases seen live on 2026-11-12
    live_cause = "Te1/1/1 on nhl-core-sw-02 went down, cutting off all downstream access switches, branch routers and hosts"
    assert repeats("Nordhafen's core switch nhl-core-sw-02 has lost its Te1/1/1 interface, causing all connected "
                   "access switches, branch routers, and hosts to lose network connectivity.", live_cause)
    assert not repeats("The outage is affecting multiple device types across the network and remains unresolved.",
                       live_cause)
    assert repeats("The branch router at Valdorina's Verona site is down, causing loss of network connectivity for "
                   "all devices relying on this router.", "bva-br-verona-rt01 node is down, causing loss of connectivity")


def test_summary_prompt_tells_the_model_not_to_restate_the_cause():
    from app.prompts.triage import SUMMARY_SYSTEM, summary_messages
    assert "do NOT restate" in SUMMARY_SYSTEM
    msgs = summary_messages({"title": "t", "severity": "major", "likely_cause": "c"}, ["x"], "Nordhafen")
    assert "do not repeat it" in msgs[1]["content"]


def test_streamed_summary_is_deduplicated_and_failure_has_its_own_text():
    cause = "Te1/1/1 on nhl-core-sw-01 went down, cutting off everything behind it"
    llm = FakeLLM([{"new": [{"customer": "nordhafen", "title": "Core uplink down", "severity": "critical",
                             "likely_cause": cause, "event_ids": ["e1"]}]}],
                  summary="Te1/1/1 on nhl-core-sw-01 went down, cutting off everything behind it. "
                          "Servers at Nordhafen are unreachable.")
    tl, *_ = loop_with(llm)
    feed(tl, [ev(1)])
    run(tl.tick())
    card = next(iter(tl.cards.values()))
    assert card.inc.summary == "Servers at Nordhafen are unreachable."

    class Failing(FakeLLM):
        def stream_text(self, role, messages, **kw):
            raise LLMError("timeout")
    llm2 = Failing([{"new": [{"customer": "nordhafen", "title": "Core uplink down", "severity": "critical",
                              "likely_cause": cause, "event_ids": ["e1"]}]}])
    tl2, *_ = loop_with(llm2)
    feed(tl2, [ev(1)])
    run(tl2.tick())
    c2 = next(iter(tl2.cards.values()))
    assert c2.inc.summary and c2.inc.summary != cause and "Alerts from 1 tool" in c2.inc.summary


# ---------------------------------------------------------------- degraded mode

def test_degraded_after_two_failures_then_probe_resumes():
    llm = FakeLLM([LLMError("timeout"), LLMError("server")])
    tl, sim, hub, real = loop_with(llm)
    feed(tl, [ev(1)])
    run(tl.tick())
    assert not tl.degraded and len(tl._carry) == 1          # first failure: retry next tick
    sim.clock.t += 8
    feed(tl, [ev(2, sec=8, host="nhl-win-02", title="Host nhl-win-02 UNREACH")])
    run(tl.tick())
    assert tl.degraded and tl.degraded_reason == "server"
    (card,) = tl.cards.values()
    assert card.inc.origin == "degraded" and card.evidence_total == 2
    assert "Network reachability" in card.inc.title and "nhl-win-0" in card.inc.next_step
    calls = len(llm.json_msgs)
    real.t += 10
    feed(tl, [ev(3, sec=16, host="nhl-win-03", title="Host nhl-win-03 UNREACH")])
    run(tl.tick())
    assert len(llm.json_msgs) == calls                       # no probe before 30 s
    real.t += 25
    feed(tl, [ev(4, sec=24, host="nhl-win-04", title="Host nhl-win-04 UNREACH")])
    llm.results = [{"attach": [{"incident_id": card.id, "event_ids": ["e1"]}]}]
    out = run(tl.tick())
    assert out.get("probe") and not tl.degraded and card.evidence_total == 4


def test_rule_based_chip_only_on_cards_whose_text_is_templated():
    """An AI card keeps its AI text and no chip through a degraded episode (alerts attached to it
    by rule-based grouping, going quiet, AI resuming); a rule-based card keeps the chip and its
    templated text stays current when the AI later attaches alerts to it."""
    cert = dict(source="checkmk", check="https_cert", host="nhl-web-02", severity="major",
                title="HTTPS certificate on nhl-web-02 EXPIRED")
    llm = FakeLLM([{"new": [{"customer": "nordhafen", "title": "Expired HTTPS certificate on nhl-web-02",
                             "severity": "major", "likely_cause": "The certificate expired.",
                             "next_step": "Renew it.", "event_ids": ["e1"]}]},
                   LLMError("timeout"), LLMError("timeout")],
                  summary="The portal is down for customers and there is no recovery yet.")
    tl, sim, hub, real = loop_with(llm)
    feed(tl, [ev(1, **cert)])
    run(tl.tick())
    (ai,) = tl.cards.values()
    assert ai.inc.origin == "llm" and tl.card_payload(ai)["rule_based"] is False
    ai_summary = ai.inc.summary
    # two failures -> degraded; the next cert alert on the same host joins the AI card,
    # unrelated reachability alerts open a rule-based card
    for k in (2, 3):
        sim.clock.t += 8
        real.t += 8
        feed(tl, [ev(k * 10, sec=8 * k, **cert),
                  ev(k * 10 + 1, sec=8 * k, host="nhl-win-01", title="Host nhl-win-01 UNREACH"),
                  ev(k * 10 + 2, sec=8 * k, host="nhl-win-02", title="Host nhl-win-02 UNREACH")])
        run(tl.tick())
    assert tl.degraded
    rb = next(c for c in tl.cards.values() if c is not ai)
    assert tl.card_payload(rb)["rule_based"] is True
    assert tl.card_payload(ai)["rule_based"] is False and ai.inc.summary == ai_summary
    # the AI card goes quiet (resolves in, idle), then the AI resumes and attaches to both
    feed(tl, [ev(90, sec=60, status="resolved", **cert)])
    tl._select(T0 + 60)
    sim.clock.t = T0 + 40 * 60
    tl._age(sim.clock.t)
    assert ai.inc.status == "quiet" and tl.card_payload(ai)["rule_based"] is False
    real.t += 60
    llm.results = [{"attach": [{"incident_id": ai.id, "event_ids": ["e1"]},
                               {"incident_id": rb.id, "event_ids": ["e2"]}]}]
    feed(tl, [ev(100, sec=40 * 60, **cert), ev(101, sec=40 * 60, host="nhl-win-01", title="Host nhl-win-01 UNREACH")])
    run(tl.tick())
    assert not tl.degraded
    by_id = {c["id"]: c for c in tl.board()}
    assert by_id[ai.id]["rule_based"] is False and by_id[rb.id]["rule_based"] is True
    assert by_id[rb.id]["summary"].startswith(f"{rb.evidence_total} alerts")      # counts kept current
    assert "Grouped by keywords" not in by_id[ai.id]["summary"]


def test_ui_chip_and_cleared_line_follow_the_payload_fields():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
    assert ':hidden="!i.rule_based" x-text="copy.ruleBased"' in html
    assert "i.origin" not in html                     # the chip never keys on the loop's mode
    assert "copy.clearedLine(clock(i.cleared_ts))" in html


def test_budget_refusal_degrades_at_once():
    llm = FakeLLM([BudgetRefused("usd_per_hour_global", 100)])
    tl, *_ = loop_with(llm)
    feed(tl, [ev(1), ev(2, host="nhl-win-02")])
    run(tl.tick())
    assert tl.degraded and tl.degraded_reason == "budget" and len(tl.board()) == 1


def test_degraded_grouping_is_deterministic():
    batch = [ev(1), ev(2, host="nhl-win-02", title="Host nhl-win-02 UNREACH"),
             ev(3, source="veeam", host="nhl-vbr-01", title="Backup job 'Daily-SQL' finished with Failed",
                check="job_failed"),
             ev(4, customer="valdorina", host="bva-sql-01", title="Blocking process on bva-sql-01", source="redgate",
                check="blocking"),
             ev(5, severity="minor", host="nhl-app-01", title="High CPU utilization (over 80% for 1m)",
                source="zabbix", check="cpu_util")]
    outs = []
    for _ in range(2):
        tl, *_ = loop_with(None)
        tl._degraded_triage(list(reversed(batch)), T0)
        outs.append(json.dumps(tl.board(), sort_keys=True))
    assert outs[0] == outs[1]
    board = json.loads(outs[0])
    assert {c["title"] for c in board} == {"Network reachability alerts at Nordhafen",
                                           "Backup and storage alerts at Nordhafen",
                                           "Database alerts at Valdorina"}      # one minor CPU blip: no card
    assert all(c["origin"] == "degraded" for c in board)
    assert family_of("Interface Te1/1/1 on nhl-core-sw-01 is down")[0] == "network"
    assert family_of("HTTPS certificate for tracking-portal has expired")[0] == "certificate"


# ---------------------------------------------------------------- idle and quiet

def test_idle_without_browser_spends_nothing():
    llm = FakeLLM()
    tl, *_ = loop_with(llm, clients=0)
    feed(tl, [ev(1)])
    assert run(tl.tick()) == {"skipped": "idle"}
    assert llm.json_msgs == [] and tl.budget.totals()["calls"] == 0


def test_quiet_stream_skips_the_model():
    llm = FakeLLM()
    tl, *_ = loop_with(llm)
    feed(tl, [ev(1, severity="warning"), ev(2, severity="info"), ev(3, status="resolved")])
    out = run(tl.tick())
    assert out["skipped"] == "quiet" and llm.json_msgs == []


def test_batch_is_capped_and_carried():
    llm = FakeLLM()
    tl, *_ = loop_with(llm)
    feed(tl, [ev(i, sec=i, host=f"nhl-win-{i:02d}", severity="minor") for i in range(80)]
         + [ev(100, sec=99, severity="critical")])
    out = run(tl.tick())
    sent = llm.json_msgs[0][1]["content"]
    assert out["lines"] == 60 and out["omitted"] == 21 and "|critical|" in sent
    assert len(tl._carry) == 21


# ---------------------------------------------------------------- dismiss endpoint

def test_dismiss_endpoint():
    from app import main
    with TestClient(main.app) as c:
        c.post("/api/reset")
        main.triage.apply_result({"new": [{"customer": "nordhafen", "title": "Core uplink down",
                                           "severity": "critical", "event_ids": ["E1"]}]}, [ev(1)])
        board = c.get("/api/incidents").json()["incidents"]
        assert len(board) == 1 and "truth" not in json.dumps(board)
        iid = board[0]["id"]
        r = c.post(f"/api/incidents/{iid}/dismiss")
        assert r.status_code == 200 and r.json()["open"] == 0
        assert c.get("/api/incidents").json()["incidents"] == []
        assert main.triage.cards[iid].inc.status == "acknowledged"      # still collects alerts silently
        assert c.post("/api/incidents/INC-nope/dismiss").status_code == 404
        c.post("/api/reset")
        assert main.triage.cards == {}
