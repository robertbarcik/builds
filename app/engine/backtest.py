"""Backtest: replay the 30-day history with and without a candidate rule (on top of the active
rules) and score the difference against the hidden ground truth.

Definitions (all counts are firing alerts; resolve events are not alerts):
- alerts_before / alerts_after: alerts reaching a human with the active rules, and with the
  active rules plus the candidate (early warnings and grouped alerts count as alerts)
- incidents_total: scenario occurrences in the history; an incident is visible if at least one
  of its root or symptom alerts survives (directly, or as a child of a surviving grouped
  alert); incidents_visible is counted with the candidate; incidents_hidden is how many
  incidents were visible with the active rules but are not with the candidate
- precursor_*: early warnings the candidate raised; a warning is true if the root of an
  incident one of its triggering alerts belongs to follows within the rule's horizon
  (`horizon_h`, default 6 h, capped at 72 h), else a false alarm; median lead = minutes from
  the warning to that root. The credit needs that causal link (a triggering alert that is part
  of the incident), so a long horizon cannot turn unrelated alerts into "right" warnings; it
  only stops a genuine 1-2 day warning (certificate expiry) from being scored as a false alarm.
- samples: 5 alerts the candidate removed (for a precursor rule: 5 warnings it raised)

The history is loaded and indexed once per process; the baseline replay for a given active
rule set is cached, so a call costs one replay.
"""
from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from app.config import HISTORY_PATH
from app.engine.rules import Engine, Ev, RuleSpec, rule_to_dict, validate
from app.models import ActiveRule, BacktestResult, PrecursorRule, Rule

TRUE_WARNING_HORIZON_S = 6 * 3600          # default credit window for an early warning
MAX_WARNING_HORIZON_S = 72 * 3600          # a rule may ask for a longer one (horizon_h), up to this
CANDIDATE_ID = "candidate"


def warning_horizon_s(rule: PrecursorRule) -> float:
    """The credit window of a precursor rule: its own `horizon_h` (capped at 72 h), else 6 h.
    Certificate expiry warnings, for example, come one to two days ahead."""
    if rule.horizon_h:
        return float(min(rule.horizon_h * 3600, MAX_WARNING_HORIZON_S))
    return float(TRUE_WARNING_HORIZON_S)


class RuleInvalid(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class History:
    events: list[Ev]
    truth: dict[str, tuple[str, Optional[str]]]           # event id -> (role, incident id)
    incidents: dict[str, frozenset[str]]                   # incident -> firing root/symptom ids
    root_t: dict[str, float]                               # incident -> first root firing time
    _baseline: dict[str, "_Run"] = field(default_factory=dict)


def history_from_dicts(rows: Iterable[dict]) -> History:
    """Index events (dicts with a `truth` block, as in history.jsonl). Sorted by time."""
    events: list[Ev] = []
    truth: dict[str, tuple[str, Optional[str]]] = {}
    inc: dict[str, set[str]] = {}
    root_t: dict[str, float] = {}
    for d in rows:
        ev = Ev.from_dict(d)
        events.append(ev)
        tr = d.get("truth") or {}
        role, iid = tr.get("role", "noise"), tr.get("incident_id")
        truth[ev.id] = (role, iid)
        if iid and ev.status == "firing":
            if role in ("root", "symptom"):
                inc.setdefault(iid, set()).add(ev.id)
            if role == "root" and (iid not in root_t or ev.t < root_t[iid]):
                root_t[iid] = ev.t
    events.sort(key=lambda e: e.t)                       # stable: keeps file order on ties
    return History(events, truth, {k: frozenset(v) for k, v in inc.items()}, root_t)


_HISTORY: dict[str, History] = {}


def load_history(path: Optional[Path] = None) -> History:
    """Load and index data/history.jsonl once per process."""
    p = str(Path(path) if path else HISTORY_PATH)
    if p not in _HISTORY:
        with open(p) as fh:
            _HISTORY[p] = history_from_dicts(json.loads(line) for line in fh if line.strip())
    return _HISTORY[p]


@dataclass
class _Run:
    alerts: int
    passed: set[str]           # original firing ids that reached a human
    visible: set[str]          # passed + children of surviving grouped alerts
    warnings: list[Ev]
    created: list[Ev]


def _replay(hist: History, specs: list[RuleSpec]) -> _Run:
    eng = Engine(specs)
    out: list[Ev] = []
    proc = eng.process
    for ev in hist.events:
        r = proc(ev)
        if r:
            out.extend(r)
    out.extend(eng.flush())
    passed: set[str] = set()
    visible: set[str] = set()
    warnings: list[Ev] = []
    created: list[Ev] = []
    alerts = 0
    for e in out:
        if e.status != "firing":
            continue
        alerts += 1
        if e.kind == "event":
            passed.add(e.id)
        else:
            created.append(e)
            if e.kind == "group":
                visible.update(e.children)
            elif e.kind == "warning":
                warnings.append(e)
    visible |= passed
    return _Run(alerts, passed, visible, warnings, created)


def _visible_incidents(hist: History, run: _Run) -> set[str]:
    return {iid for iid, ids in hist.incidents.items() if not ids.isdisjoint(run.visible)}


def _as_specs(active: Iterable[Union[ActiveRule, dict, Rule, tuple]]) -> list[RuleSpec]:
    specs: list[RuleSpec] = []
    for i, a in enumerate(active):
        if isinstance(a, tuple):
            specs.append(a)
        elif isinstance(a, ActiveRule):
            if a.enabled:
                specs.append((a.id, a.title, a.rule))
        else:
            rule, errs = validate(a)
            if errs:
                raise RuleInvalid([f"active rule {i}: {e}" for e in errs])
            specs.append((f"active-{i}", f"Active rule {i}", rule))
    return specs


def _spec_key(specs: list[RuleSpec]) -> str:
    return json.dumps([(rid, rule_to_dict(r)) for rid, _, r in specs], sort_keys=True)


def _spread(items: list[Any], n: int) -> list[Any]:
    """n items spread over the list, deterministic (seeded by the list length), time order."""
    if len(items) <= n:
        return items
    rng = random.Random(f"samples:{len(items)}")
    step = len(items) / n
    return [items[int(i * step + rng.random() * step)] for i in range(n)]


def backtest(rule_in: Union[dict, str, Rule], active_rules: Optional[Iterable[Any]] = None,
             history: Optional[History] = None) -> BacktestResult:
    """Score one candidate rule against the history.

    `active_rules`: None = the store's enabled rules; otherwise a list of ActiveRule, rule
    dicts or (id, title, rule) tuples. Raises RuleInvalid(errors) for an invalid candidate."""
    rule, errs = validate(rule_in)
    if errs:
        raise RuleInvalid(errs)
    if active_rules is None:
        from app.engine.store import get_store
        active_rules = get_store().enabled()
    specs = _as_specs(active_rules)
    hist = history or load_history()

    key = _spec_key(specs)
    base = hist._baseline.get(key)
    if base is None:
        base = _replay(hist, specs)
        if len(hist._baseline) > 16:
            hist._baseline.clear()
        hist._baseline[key] = base
    after = _replay(hist, specs + [(CANDIDATE_ID, "Candidate", rule)])

    vis_before = _visible_incidents(hist, base)
    vis_after = _visible_incidents(hist, after)
    saved = base.alerts - after.alerts

    res: dict[str, Any] = dict(
        alerts_before=base.alerts, alerts_after=after.alerts, alerts_saved=saved,
        saved_pct=(round(100.0 * saved / base.alerts, 1) + 0.0) if base.alerts else 0.0,
        incidents_total=len(hist.incidents), incidents_visible=len(vis_after),
        incidents_hidden=len(vis_before - vis_after),
    )
    if isinstance(rule, PrecursorRule):
        mine = [w for w in after.warnings if w.produced_by == CANDIDATE_ID]
        horizon = warning_horizon_s(rule)
        leads: list[float] = []
        n_true = 0
        for w in mine:
            best = None
            for cid in w.children:
                iid = hist.truth.get(cid, ("noise", None))[1]
                rt = hist.root_t.get(iid) if iid else None
                if rt is not None and w.t <= rt <= w.t + horizon:
                    lead = (rt - w.t) / 60
                    best = lead if best is None else min(best, lead)
            if best is not None:
                n_true += 1
                leads.append(best)
        res.update(precursor_fired=len(mine), precursor_true=n_true, precursor_false=len(mine) - n_true,
                   median_lead_min=round(statistics.median(leads), 1) if leads else None)
        res["samples"] = [f"raised: {w.line()}  (after {len(w.children)} alerts)" for w in _spread(mine, 5)]
    else:
        gone = [e for e in hist.events if e.id in base.passed and e.id not in after.passed]
        res["samples"] = [e.line() for e in _spread(gone, 5)]
    return BacktestResult(**res)


def backtest_set(rules_in: list[Union[dict, Rule]], active_rules: Optional[Iterable[Any]] = None,
                 history: Optional[History] = None) -> BacktestResult:
    """Score several candidate rules together on top of the active rules (one replay with all of
    them): the combined figure for a set of proposals. Precursor statistics are not computed."""
    cands: list[RuleSpec] = []
    for i, r in enumerate(rules_in):
        rule, errs = validate(r)
        if errs:
            raise RuleInvalid([f"rule {i}: {e}" for e in errs])
        cands.append((f"{CANDIDATE_ID}-{i}", f"Candidate {i}", rule))
    if active_rules is None:
        from app.engine.store import get_store
        active_rules = get_store().enabled()
    specs = _as_specs(active_rules)
    hist = history or load_history()
    key = _spec_key(specs)
    base = hist._baseline.get(key)
    if base is None:
        base = hist._baseline[key] = _replay(hist, specs)
    after = _replay(hist, specs + cands)
    vis_before, vis_after = _visible_incidents(hist, base), _visible_incidents(hist, after)
    saved = base.alerts - after.alerts
    return BacktestResult(
        alerts_before=base.alerts, alerts_after=after.alerts, alerts_saved=saved,
        saved_pct=(round(100.0 * saved / base.alerts, 1) + 0.0) if base.alerts else 0.0,
        incidents_total=len(hist.incidents), incidents_visible=len(vis_after),
        incidents_hidden=len(vis_before - vis_after))


def backtest_tool(rule_in: Union[dict, str], active_rules: Optional[Iterable[Any]] = None) -> dict:
    """Agent-friendly wrapper: {"ok": true, "backtest": {...}} or {"ok": false, "errors": [...]}."""
    try:
        bt = backtest(rule_in, active_rules)
    except RuleInvalid as e:
        return {"ok": False, "errors": e.errors}
    return {"ok": True, "backtest": bt.model_dump()}
