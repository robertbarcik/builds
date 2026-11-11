"""Live wiring: the active rule set filters the simulator's stream.

`RuleRuntime` plugs into `LiveSim.rules_hook`. For every raw event the sim calls
`process(event) -> (events that reach a human, events dropped)`; precursor and correlate
outputs come back as new Events with `produced_by` set. Toggling or adding a rule rebuilds the
engine (alerts the old engine was holding are released, not lost) and publishes `rule_changed`.
Per-rule live counts survive rebuilds and are reset by `reset()`.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from app.engine.rules import Engine, Ev, describe, rule_to_dict
from app.engine.store import RuleStore
from app.models import ActiveRule, Event, Rule

KIND_LABEL = {"suppress": "Suppress", "precursor": "Early warning", "retune": "Retune", "correlate": "Group"}


class RuleRuntime:
    def __init__(self, store: RuleStore, sim: Any, hub: Any):
        self.store = store
        self.sim = sim
        self.hub = hub
        self._base: dict[str, dict[str, int]] = {}      # counts carried over from earlier engines
        self._carry: list[Ev] = []                      # released by a rebuild, emitted next call
        self.engine = Engine(store.specs())
        sim.rules_hook = self

    # ---------------------------------------------------------- engine lifecycle
    def _rebuild(self, keep_counts: bool = True) -> None:
        if keep_counts:
            self._carry.extend(self.engine.flush())
            for rid, c in self.engine.stats().items():
                b = self._base.setdefault(rid, {"filtered": 0, "raised": 0})
                b["filtered"] += c["filtered"]
                b["raised"] += c["raised"]
        else:
            self._carry = []
            self._base = {}
        self.engine = Engine(self.store.specs())

    # ---------------------------------------------------------- stream hook (LiveSim)
    def process(self, ev: Event) -> tuple[list[Event], int]:
        before = self.engine.events_dropped
        outs = self._carry + self.engine.process(Ev.from_event(ev))
        self._carry = []
        return [o.to_event() for o in outs], self.engine.events_dropped - before

    def advance(self, now: float) -> tuple[list[Event], int]:
        before = self.engine.events_dropped
        outs = self._carry + self.engine.advance(now)
        self._carry = []
        return [o.to_event() for o in outs], self.engine.events_dropped - before

    # ---------------------------------------------------------- payloads (no truth)
    def counts(self) -> dict[str, dict[str, int]]:
        cur = self.engine.stats()
        out: dict[str, dict[str, int]] = {}
        for r in self.store.rules:
            b = self._base.get(r.id, {"filtered": 0, "raised": 0})
            c = cur.get(r.id, {"filtered": 0, "raised": 0})
            f, rz = b["filtered"] + c["filtered"], b["raised"] + c["raised"]
            # a grouped alert replaces its children: net alerts saved
            out[r.id] = {"filtered": max(0, f - rz) if r.rule.kind == "correlate" else f, "raised": rz}
        return out

    def rule_payload(self, r: ActiveRule, counts: Optional[dict] = None) -> dict[str, Any]:
        c = (counts or self.counts()).get(r.id, {"filtered": 0, "raised": 0})
        return {"id": r.id, "title": r.title, "kind": r.rule.kind, "kind_label": KIND_LABEL[r.rule.kind],
                "description": describe(r.rule), "enabled": r.enabled, "live_since_ts": r.live_since_ts,
                "proposal_id": r.proposal_id, "seed": r.id.startswith("seed-"), "rule": rule_to_dict(r.rule),
                "filtered": c["filtered"], "raised": c["raised"]}

    def payload(self) -> list[dict[str, Any]]:
        counts = self.counts()
        return [self.rule_payload(r, counts) for r in self.store.rules]

    def tick_info(self) -> dict[str, Any]:
        return {"rule_counts": self.counts(), "rules_active": sum(1 for r in self.store.rules if r.enabled)}

    def _publish(self, reason: str, rule_id: Optional[str] = None) -> None:
        self.hub.publish("rule_changed", {"reason": reason, "rule_id": rule_id, "rules": self.payload()})

    # ---------------------------------------------------------- changes
    def set_enabled(self, rid: str, enabled: Optional[bool] = None) -> ActiveRule:
        r = self.store.get(rid)
        if r is None:
            raise KeyError(rid)
        want = (not r.enabled) if enabled is None else bool(enabled)
        if want != r.enabled:
            self.store.set_enabled(rid, want)
            self._rebuild()
            self.sim.add_marker(f"{r.title} {'on' if want else 'off'}")
        self._publish("toggle", rid)
        return r

    def activate(self, rule: Union[Rule, dict], title: str, proposal_id: Optional[str] = None) -> ActiveRule:
        """Put a rule live now (accepted proposal). Validates; raises ValueError with errors."""
        ar = self.store.add(rule, title, proposal_id=proposal_id,
                            live_since_ts=self.sim.tick_payload()["sim_ts"])
        self._rebuild()
        self.sim.add_marker(ar.title)
        self._publish("activate", ar.id)
        return ar

    def reset(self) -> None:
        """Seed rules only, fresh engine, counts zeroed (called with the sim reset)."""
        self.store.reset_to_seed()
        self._rebuild(keep_counts=False)
        self._publish("reset")
