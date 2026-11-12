"""Rule DSL: validation, plain-English descriptions and the deterministic evaluator.

Four kinds share one `match` selector (see app/models.py):
- suppress   drop matching alerts beyond `max_per_window` per host per window (all if 0)
- precursor  raise one early-warning event when `count` matching alerts fall within
             `within_min` at one customer; never drops anything
- retune     keep a source/check alert only if its value breaches `new_threshold` for at
             least `sustain_min` (a resolve inside that time drops it)
- correlate  member alerts at one customer within `within_min` collapse into one grouped
             alert that keeps the child ids (a lone member alert passes unchanged)

The evaluator is streaming: `Engine.process(ev)` takes one event at a time in time order and
returns what reaches a human (originals, early warnings, grouped alerts). Retune with
`sustain_min` and correlate hold alerts for a while; held alerts come out when a later event
(or `advance(now)`) moves time past their deadline, and `flush()` releases the rest. Replaying
the history through `replay()` is exactly this loop, so live and backtest agree.

A "resolved" event is paired FIFO with the open firing of the same (customer, source, host,
check, title) that went through the same rule; if that firing was dropped, the resolve is too.
Precursor rules watch the raw stream (before any other rule drops anything). Dropping rules run
in list order; a later rule sees only what earlier rules let through.
"""
from __future__ import annotations

import heapq
import json
import math
import re
from collections import deque
from datetime import datetime
from fnmatch import translate
from typing import Any, Callable, Iterable, Optional, Union

from pydantic import TypeAdapter, ValidationError

from app.models import (SEVERITIES, CorrelateRule, Event, Match, PrecursorRule, RetuneRule, Rule,
                        SuppressRule)
from app.sim.catalog import CUSTOMERS, SOURCES, TEMPLATES

KINDS = ("suppress", "precursor", "retune", "correlate")
SEV_RANK = {"info": 0, "warning": 1, "minor": 2, "major": 3, "critical": 4}
RULE_VENDOR = "Signal Desk rule"
RULE_SOURCE = "signal_desk"

_RULE_ADAPTER: TypeAdapter = TypeAdapter(Rule)

_MATCH_KEYS = ("source", "customer", "host_glob", "title_contains", "severity_in")
_KIND_KEYS = {
    "suppress": ("kind", "match", "window_min", "max_per_window"),
    "precursor": ("kind", "match", "count", "within_min", "raise", "horizon_h"),
    "retune": ("kind", "source", "check", "op", "new_threshold", "sustain_min"),
    "correlate": ("kind", "members", "within_min", "keep"),
}
_RAISE_KEYS = ("title", "severity")
_MINUTE_KEYS = {"window_min", "within_min"}


# =============================================================== validation

def _loc_str(loc: Iterable[Any]) -> str:
    out = ""
    for p in loc:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out


def _allowed_for(parent: str, kind: str) -> tuple[str, ...]:
    last = re.sub(r"\[\d+\]$", "", parent.split(".")[-1]) if parent else ""
    if last in ("match", "members"):
        return _MATCH_KEYS
    if last == "raise":
        return _RAISE_KEYS
    return _KIND_KEYS.get(kind, ())


def _pydantic_errors(exc: ValidationError, kind: str) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = list(err.get("loc", ()))
        if loc and loc[0] == kind:          # discriminated-union tag
            loc = loc[1:]
        path = _loc_str(loc)
        last = str(loc[-1]) if loc else ""
        etype = err.get("type", "")
        if etype == "extra_forbidden":
            parent = _loc_str(loc[:-1])
            where = f" in {parent}" if parent else ""
            allowed = ", ".join(_allowed_for(parent, kind))
            out.append(f"unknown key '{last}'{where}; allowed: {allowed}")
        elif etype == "missing":
            out.append(f"missing key '{path}'")
        elif last in _MINUTE_KEYS and etype in ("greater_than_equal", "less_than_equal", "int_parsing",
                                                 "int_from_float", "int_type"):
            out.append(f"{path} must be a whole number of minutes between 1 and 1440")
        elif last == "sustain_min" and etype in ("greater_than_equal", "less_than_equal"):
            out.append("sustain_min must be between 0 and 1440 minutes")
        elif last == "horizon_h" and etype in ("greater_than_equal", "less_than_equal", "int_parsing",
                                               "int_from_float", "int_type"):
            out.append("horizon_h must be a whole number of hours between 1 and 72")
        elif path == "members" and etype in ("too_short", "too_long"):
            out.append("members must list 2 to 4 matches")
        elif etype == "literal_error" and "severity" in path:
            out.append(f"{path}: severity must be one of {', '.join(SEVERITIES)}")
        else:
            msg = err.get("msg", "invalid").rstrip(".")
            out.append(f"{path}: {msg[0].lower() + msg[1:] if msg else msg}" if path else msg)
    return out


def _check_match(m: Match, where: str) -> list[str]:
    errs: list[str] = []
    if m.source is not None and m.source not in SOURCES:
        errs.append(f"{where}.source '{m.source}' is not a known source; use one of: {', '.join(SOURCES)}")
    if m.customer is not None and m.customer not in CUSTOMERS:
        errs.append(f"{where}.customer '{m.customer}' is not a known customer; use one of: {', '.join(CUSTOMERS)}")
    if m.title_contains is not None and len(m.title_contains.strip()) < 3:
        errs.append(f"{where}.title_contains must be at least 3 characters")
    if m.host_glob is not None and (not m.host_glob.strip() or set(m.host_glob) <= set("*?")):
        errs.append(f"{where}.host_glob '{m.host_glob}' matches every host; name a host pattern")
    if m.severity_in is not None and len(m.severity_in) == 0:
        errs.append(f"{where}.severity_in must not be empty")
    narrowing = m.source or (m.host_glob and m.host_glob.strip()) or (m.title_contains and m.title_contains.strip())
    if not narrowing:
        errs.append(f"{where} matches too much: set source, host_glob or title_contains")
    return errs


def _can_be_critical(m: Match) -> bool:
    if m.severity_in is not None:
        return "critical" in m.severity_in
    if m.source in SOURCES:
        return "critical" in SOURCES[m.source].severity_map.values()
    return True


def numeric_checks(source: str) -> list[str]:
    return [c for (s, c), t in TEMPLATES.items() if s == source and t.threshold is not None]


def _semantic_errors(rule: Any) -> list[str]:
    errs: list[str] = []
    if isinstance(rule, SuppressRule):
        errs += _check_match(rule.match, "match")
        if not errs and _can_be_critical(rule.match):
            errs.append("suppress could drop critical alerts: add severity_in without 'critical'")
    elif isinstance(rule, PrecursorRule):
        errs += _check_match(rule.match, "match")
    elif isinstance(rule, RetuneRule):
        if rule.source not in SOURCES:
            errs.append(f"source '{rule.source}' is not a known source; use one of: {', '.join(SOURCES)}")
        elif (rule.source, rule.check) not in TEMPLATES:
            known = [c for (s, c) in TEMPLATES if s == rule.source]
            errs.append(f"check '{rule.check}' is not a check of {rule.source}; known: {', '.join(known)}")
        elif TEMPLATES[(rule.source, rule.check)].threshold is None:
            nums = numeric_checks(rule.source)
            errs.append(f"check '{rule.check}' of {rule.source} carries no numeric value; retune works on: "
                        f"{', '.join(nums) if nums else 'nothing in this source'}")
        if not math.isfinite(rule.new_threshold):
            errs.append("new_threshold must be a finite number")
    elif isinstance(rule, CorrelateRule):
        for i, m in enumerate(rule.members):
            errs += _check_match(m, f"members[{i}]")
        if rule.keep >= len(rule.members):
            errs.append(f"keep must be an index into members (0 to {len(rule.members) - 1})")
        dumps = [json.dumps(m.model_dump(exclude_none=True), sort_keys=True) for m in rule.members]
        if len(set(dumps)) < len(dumps):
            errs.append("members must differ from each other")
    return errs


def validate(rule_in: Union[dict, str, Rule, Any]) -> tuple[Optional[Rule], list[str]]:
    """Return (rule, []) or (None, [short error strings an LLM can act on])."""
    if isinstance(rule_in, (SuppressRule, PrecursorRule, RetuneRule, CorrelateRule)):
        rule_in = rule_in.model_dump(by_alias=True)
    if isinstance(rule_in, str):
        try:
            rule_in = json.loads(rule_in)
        except ValueError:
            return None, ["rule is not valid JSON"]
    if not isinstance(rule_in, dict):
        return None, ["rule must be a JSON object"]
    kind = rule_in.get("kind")
    if kind not in KINDS:
        return None, [f"kind must be one of: {', '.join(KINDS)}"]
    try:
        rule = _RULE_ADAPTER.validate_python(rule_in)
    except ValidationError as exc:
        return None, _pydantic_errors(exc, kind)
    errs = _semantic_errors(rule)
    return (None, errs) if errs else (rule, [])


def rule_to_dict(rule: Rule) -> dict:
    return rule.model_dump(by_alias=True, exclude_none=True)


# =============================================================== plain English

def _vendor(source: Optional[str]) -> str:
    return SOURCES[source].vendor if source in SOURCES else (source or "")


def _minutes(n: int) -> str:
    if n % 1440 == 0:
        d = n // 1440
        return "day" if d == 1 else f"{d} days"
    if n % 60 == 0:
        h = n // 60
        return "hour" if h == 1 else f"{h} hours"
    return "minute" if n == 1 else f"{n} minutes"


def _within(n: int) -> str:
    w = _minutes(n)
    return w if w[0].isdigit() else f"1 {w}"


def describe_match(m: Match, plural: bool = True) -> str:
    parts = []
    if m.severity_in:
        parts.append("/".join(m.severity_in))
    parts.append(_vendor(m.source) if m.source else "any-source")
    noun = "alerts" if plural else "alert"
    s = " ".join(parts) + f" {noun}"
    if m.title_contains:
        s += f" containing “{m.title_contains}”"
    if m.host_glob:
        s += f" on hosts like {m.host_glob}"
    if m.customer:
        s += f" at {CUSTOMERS[m.customer].name if m.customer in CUSTOMERS else m.customer}"
    return s


def describe(rule: Rule) -> str:
    """One deterministic plain-English sentence saying what the rule does."""
    if isinstance(rule, SuppressRule):
        what = describe_match(rule.match)
        if rule.max_per_window == 0:
            return f"Hides {what}."
        per = "1 alert" if rule.max_per_window == 1 else f"{rule.max_per_window} alerts"
        return f"Lets {per} per host through every {_minutes(rule.window_min)} for {what}; hides the repeats."
    if isinstance(rule, PrecursorRule):
        what = describe_match(rule.match)
        ahead = (f" Expects the failure within {rule.horizon_h} hour{'s' if rule.horizon_h != 1 else ''}."
                 if rule.horizon_h else "")
        return (f"Raises an early warning “{rule.raise_.title}” ({rule.raise_.severity}) when "
                f"{rule.count} {what} fire within {_within(rule.within_min)} at one customer.{ahead} Hides nothing.")
    if isinstance(rule, RetuneRule):
        tpl = TEMPLATES.get((rule.source, rule.check))
        unit = (tpl.unit.strip() if tpl else "") or ""
        thr = f"{rule.new_threshold:g}{unit if unit == '%' else (' ' + unit if unit else '')}"
        ops = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below"}[rule.op]
        sustain = f" for at least {_within(rule.sustain_min)}" if rule.sustain_min else ""
        return f"Keeps {_vendor(rule.source)} “{rule.check}” alerts only when the value stays {ops} {thr}{sustain}."
    if isinstance(rule, CorrelateRule):
        names = [describe_match(m, plural=False) for m in rule.members]
        return (f"Groups {', '.join(names[:-1])} and {names[-1]} firing at one customer within "
                f"{_within(rule.within_min)} into one alert.")
    return ""


# =============================================================== evaluator

def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class Ev:
    """Lightweight event the evaluator works on. `orig` points back to the live Event (if any).
    `kind` is "event" for originals, "warning" for precursor output, "group" for correlate output."""

    __slots__ = ("id", "t", "ts", "customer", "source", "vendor", "host", "service", "check", "title",
                 "severity", "native_severity", "status", "value", "threshold", "produced_by", "children",
                 "kind", "rule_title", "base_title", "orig")

    def __init__(self, id: str, t: float, ts: str, customer: str, source: str, vendor: str, host: str,
                 service: str, check: str, title: str, severity: str, native_severity: str, status: str,
                 value: Optional[float] = None, threshold: Optional[float] = None,
                 produced_by: Optional[str] = None, children: tuple = (), kind: str = "event",
                 rule_title: Optional[str] = None, orig: Any = None):
        self.id = id
        self.t = t
        self.ts = ts
        self.customer = customer
        self.source = source
        self.vendor = vendor
        self.host = host
        self.service = service
        self.check = check
        self.title = title
        self.severity = severity
        self.native_severity = native_severity
        self.status = status
        self.value = value
        self.threshold = threshold
        self.produced_by = produced_by
        self.children = tuple(children)
        self.kind = kind
        self.rule_title = rule_title
        self.base_title = title[10:] if status == "resolved" and title.startswith("Resolved: ") else title
        self.orig = orig

    @classmethod
    def from_dict(cls, d: dict, orig: Any = None) -> "Ev":
        return cls(d["id"], _epoch(d["ts"]), d["ts"], d["customer"], d["source"], d.get("vendor", d["source"]),
                   d["host"], d.get("service", ""), d.get("check", ""), d["title"], d["severity"],
                   d.get("native_severity", d["severity"]), d.get("status", "firing"), d.get("value"),
                   d.get("threshold"), d.get("produced_by"), tuple(d.get("children") or ()),
                   orig=orig)

    @classmethod
    def from_event(cls, e: Event) -> "Ev":
        return cls(e.id, _epoch(e.ts), e.ts, e.customer, e.source, e.vendor, e.host, e.service, e.check,
                   e.title, e.severity, e.native_severity, e.status, e.value, e.threshold, e.produced_by,
                   tuple(e.children), orig=e)

    @property
    def key(self) -> tuple:
        return (self.customer, self.source, self.host, self.check, self.base_title)

    def to_event(self) -> Event:
        """The live Event: the original if there is one, else a new one built for a rule output."""
        if self.orig is not None and self.kind == "event":
            return self.orig
        raw = {"rule_id": self.produced_by, "rule_title": self.rule_title}
        if self.kind == "group":
            raw["grouped"] = len(self.children)
        return Event(id=self.id, ts=self.ts, customer=self.customer, source=self.source, vendor=self.vendor,
                     host=self.host, service=self.service, check=self.check, title=self.title,
                     severity=self.severity, native_severity=self.native_severity, status=self.status,
                     value=self.value, threshold=self.threshold, raw=raw, produced_by=self.produced_by,
                     children=list(self.children))

    def line(self) -> str:
        sev = self.native_severity
        return f"{self.ts[:16].replace('T', ' ')}  {self.vendor}  {self.host}  {sev}  {self.title}"


def compile_match(m: Match) -> Callable[[Ev], bool]:
    src, cust = m.source, m.customer
    host_re = re.compile(translate(m.host_glob)).match if m.host_glob else None
    needle = m.title_contains.lower() if m.title_contains else None
    sevs = frozenset(m.severity_in) if m.severity_in else None

    def match(ev: Ev) -> bool:
        if src is not None and ev.source != src:
            return False
        if cust is not None and ev.customer != cust:
            return False
        if sevs is not None and ev.severity not in sevs:
            return False
        if host_re is not None and not host_re(ev.host):
            return False
        if needle is not None and needle not in ev.base_title.lower():
            return False
        return True

    return match


class _Fate:
    __slots__ = ("state", "ev", "resolves", "member", "deadline")

    def __init__(self, state: str, ev: Optional[Ev] = None):
        self.state = state            # pass | drop | held
        self.ev = ev
        self.resolves: list[Ev] = []
        self.member = 0
        self.deadline = 0.0


class _Stage:
    """A dropping rule. Subclasses implement `_fire` and may hold alerts until `advance`."""

    FATE_DEPTH = 64

    def __init__(self, rid: str, title: str, rule: Any):
        self.rid = rid
        self.title = title
        self.rule = rule
        self.alerts_dropped = 0       # firing alerts this rule removed
        self.events_dropped = 0       # firing + resolved events removed (noise strip)
        self.raised = 0               # events this rule created (grouped alerts)
        self._fates: dict[tuple, deque] = {}

    def _record(self, ev: Ev, state: str) -> _Fate:
        f = _Fate(state, ev)
        dq = self._fates.get(ev.key)
        if dq is None:
            dq = self._fates[ev.key] = deque(maxlen=self.FATE_DEPTH)
        dq.append(f)
        return f

    def _drop(self, n_alerts: int = 1, n_events: int = 1) -> None:
        self.alerts_dropped += n_alerts
        self.events_dropped += n_events

    def push(self, ev: Ev) -> list[Ev]:
        if ev.status == "resolved":
            dq = self._fates.get(ev.key)
            if not dq:
                return [ev]
            return self._resolve(dq.popleft(), ev)
        return self._fire(ev)

    def _resolve(self, f: _Fate, ev: Ev) -> list[Ev]:
        if f.state == "pass":
            return [ev]
        if f.state == "drop":
            self._drop(0, 1)
            return []
        f.resolves.append(ev)          # held: decided together with its firing
        return []

    def _fire(self, ev: Ev) -> list[Ev]:
        raise NotImplementedError

    def advance(self, now: float) -> list[Ev]:
        return []

    def held(self) -> int:
        return 0


class SuppressStage(_Stage):
    def __init__(self, rid: str, title: str, rule: SuppressRule):
        super().__init__(rid, title, rule)
        self.match = compile_match(rule.match)
        self.window = rule.window_min * 60
        self.max = rule.max_per_window
        self._passed: dict[tuple, deque] = {}

    def _fire(self, ev: Ev) -> list[Ev]:
        if ev.severity == "critical" or not self.match(ev):
            return [ev]
        if self.max > 0:
            hk = (ev.customer, ev.host)
            dq = self._passed.get(hk)
            if dq is None:
                dq = self._passed[hk] = deque()
            cutoff = ev.t - self.window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) < self.max:
                dq.append(ev.t)
                self._record(ev, "pass")
                return [ev]
        self._record(ev, "drop")
        self._drop()
        return []


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda v, t: v > t, ">=": lambda v, t: v >= t, "<": lambda v, t: v < t, "<=": lambda v, t: v <= t,
}


class RetuneStage(_Stage):
    def __init__(self, rid: str, title: str, rule: RetuneRule):
        super().__init__(rid, title, rule)
        self.op = _OPS[rule.op]
        self.thr = rule.new_threshold
        self.sustain = rule.sustain_min * 60
        self._heap: list[tuple[float, int, _Fate]] = []
        self._seq = 0

    def _fire(self, ev: Ev) -> list[Ev]:
        r = self.rule
        if ev.source != r.source or ev.check != r.check or ev.value is None:
            return [ev]
        if not self.op(ev.value, self.thr):
            self._record(ev, "drop")
            self._drop()
            return []
        if self.sustain <= 0:
            self._record(ev, "pass")
            return [ev]
        f = self._record(ev, "held")
        f.deadline = ev.t + self.sustain
        self._seq += 1
        heapq.heappush(self._heap, (f.deadline, self._seq, f))
        return []

    def _resolve(self, f: _Fate, ev: Ev) -> list[Ev]:
        if f.state == "held":          # cleared before it was sustained: drop both
            f.state = "drop"
            self._drop(1, 2)
            return []
        return super()._resolve(f, ev)

    def advance(self, now: float) -> list[Ev]:
        out: list[Ev] = []
        while self._heap and self._heap[0][0] <= now:
            _, _, f = heapq.heappop(self._heap)
            if f.state == "held":
                f.state = "pass"
                out.append(f.ev)
        return out

    def held(self) -> int:
        return sum(1 for _, _, f in self._heap if f.state == "held")


class _Group:
    __slots__ = ("customer", "deadline", "items")

    def __init__(self, customer: str, deadline: float):
        self.customer = customer
        self.deadline = deadline
        self.items: list[_Fate] = []


class CorrelateStage(_Stage):
    def __init__(self, rid: str, title: str, rule: CorrelateRule):
        super().__init__(rid, title, rule)
        self.members = [compile_match(m) for m in rule.members]
        self.window = rule.within_min * 60
        self.keep = rule.keep
        self._open: dict[str, _Group] = {}
        self._heap: list[tuple[float, int, _Group]] = []
        self._seq = 0

    def _fire(self, ev: Ev) -> list[Ev]:
        idx = next((i for i, m in enumerate(self.members) if m(ev)), None)
        if idx is None:
            return [ev]
        g = self._open.get(ev.customer)
        if g is None or ev.t > g.deadline:
            g = _Group(ev.customer, ev.t + self.window)
            self._open[ev.customer] = g
            self._seq += 1
            heapq.heappush(self._heap, (g.deadline, self._seq, g))
        f = self._record(ev, "held")
        f.member = idx
        g.items.append(f)
        return []

    def _close(self, g: _Group) -> list[Ev]:
        if len(g.items) == 1:
            f = g.items[0]
            f.state = "pass"
            out, f.resolves = [f.ev] + f.resolves, []
            return out
        head = next((f for f in g.items if f.member == self.keep), g.items[0]).ev
        first = g.items[0].ev
        n = len(g.items)
        sev = max((f.ev.severity for f in g.items), key=lambda s: SEV_RANK[s])
        grouped = Ev(f"{self.rid}:{head.id}", first.t, first.ts, head.customer, head.source, head.vendor,
                     head.host, head.service, head.check, f"{head.title} (+{n - 1} related, grouped)", sev,
                     head.native_severity, "firing", head.value, head.threshold, produced_by=self.rid,
                     children=tuple(f.ev.id for f in g.items), kind="group", rule_title=self.title)
        for f in g.items:
            f.state = "drop"
            self._drop(1, 1 + len(f.resolves))
            f.resolves = []
        self.raised += 1
        return [grouped]

    def advance(self, now: float) -> list[Ev]:
        out: list[Ev] = []
        while self._heap and self._heap[0][0] <= now:
            _, _, g = heapq.heappop(self._heap)
            if self._open.get(g.customer) is g:
                del self._open[g.customer]
            out.extend(self._close(g))
        return out

    def held(self) -> int:
        return sum(len(g.items) + sum(len(f.resolves) for f in g.items) for _, _, g in self._heap)


class PrecursorWatch:
    """Observes the raw stream; emits one early warning per run of `count` matches."""

    def __init__(self, rid: str, title: str, rule: PrecursorRule):
        self.rid = rid
        self.title = title
        self.rule = rule
        self.match = compile_match(rule.match)
        self.window = rule.within_min * 60
        self.count = rule.count
        self.raised = 0
        self.alerts_dropped = 0
        self.events_dropped = 0
        self._recent: dict[str, deque] = {}
        self._quiet_until: dict[str, float] = {}

    def observe(self, ev: Ev) -> list[Ev]:
        if ev.status != "firing" or ev.kind != "event" or not self.match(ev):
            return []
        c = ev.customer
        if ev.t < self._quiet_until.get(c, -math.inf):
            return []
        dq = self._recent.get(c)
        if dq is None:
            dq = self._recent[c] = deque()
        dq.append(ev)
        cutoff = ev.t - self.window
        while dq and dq[0].t < cutoff:
            dq.popleft()
        if len(dq) < self.count:
            return []
        kids = tuple(e.id for e in dq)
        dq.clear()
        self._quiet_until[c] = ev.t + self.window
        self.raised += 1
        sev = self.rule.raise_.severity
        w = Ev(f"{self.rid}:w{self.raised}:{ev.id}", ev.t, ev.ts, c, RULE_SOURCE, RULE_VENDOR, ev.host, ev.service,
               "early_warning", f"Early warning: {self.rule.raise_.title}", sev, sev.capitalize(), "firing",
               produced_by=self.rid, children=kids, kind="warning", rule_title=self.title)
        return [w]


def make_stage(rid: str, title: str, rule: Rule):
    if isinstance(rule, SuppressRule):
        return SuppressStage(rid, title, rule)
    if isinstance(rule, RetuneRule):
        return RetuneStage(rid, title, rule)
    if isinstance(rule, CorrelateRule):
        return CorrelateStage(rid, title, rule)
    if isinstance(rule, PrecursorRule):
        return PrecursorWatch(rid, title, rule)
    raise TypeError(f"not a rule: {rule!r}")


RuleSpec = tuple[str, str, Rule]      # (rule id, title, rule)


class Engine:
    """Stateful, streaming rule evaluator for one ordered set of rules."""

    def __init__(self, rules: Iterable[RuleSpec] = ()):
        self.watches: list[PrecursorWatch] = []
        self.stages: list[_Stage] = []
        for rid, title, rule in rules:
            st = make_stage(rid, title, rule)
            (self.watches if isinstance(st, PrecursorWatch) else self.stages).append(st)

    def _run(self, start: int, evs: list[Ev]) -> list[Ev]:
        for st in self.stages[start:]:
            if not evs:
                break
            nxt: list[Ev] = []
            for e in evs:
                nxt.extend(st.push(e))
            evs = nxt
        return evs

    def advance(self, now: float) -> list[Ev]:
        out: list[Ev] = []
        for i, st in enumerate(self.stages):
            rel = st.advance(now)
            if rel:
                out.extend(self._run(i + 1, rel))
        return out

    def process(self, ev: Ev) -> list[Ev]:
        """Feed one event (time order). Returns what reaches a human now."""
        out = self.advance(ev.t)
        for w in self.watches:
            out.extend(w.observe(ev))
        out.extend(self._run(0, [ev]))
        return out

    def flush(self) -> list[Ev]:
        return self.advance(math.inf)

    def held(self) -> int:
        return sum(st.held() for st in self.stages)

    @property
    def events_dropped(self) -> int:
        return sum(st.events_dropped for st in self.stages)

    @property
    def alerts_dropped(self) -> int:
        return sum(st.alerts_dropped for st in self.stages)

    def stats(self) -> dict[str, dict[str, int]]:
        """Per rule: firing alerts removed and events created, cumulative."""
        return {st.rid: {"filtered": st.alerts_dropped, "raised": st.raised}
                for st in [*self.watches, *self.stages]}


def replay(events: Iterable[Ev], rules: Iterable[RuleSpec]) -> list[Ev]:
    """Batch replay: the streaming loop over a time-ordered list, then flush."""
    eng = Engine(rules)
    out: list[Ev] = []
    for ev in events:
        out.extend(eng.process(ev))
    out.extend(eng.flush())
    return out
