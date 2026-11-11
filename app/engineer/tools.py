"""The rule engineer's tools: small, compact views over the indexed 30-day history.

Every tool works on what a real desk can see: the alerts that reach the operators after the
active rules (`passed` in the baseline replay) and the ServiceNow incident tickets in that
stream. None of them reads the hidden ground truth (`History.truth`, `incidents`, `root_t`);
only `backtest` does, through the deterministic engine, and it returns aggregate counts (alerts
saved, incidents on record still visible, early warnings that were right), never a per-event
label. `assert_clean()` checks every output for truth-shaped keys, and every output is capped
at `MAX_OUTPUT_CHARS` (rows are dropped from the end, `"truncated": true` says so).

`run_tool(name, args, ctx)` returns (output dict for the model, one human-readable work-log
line for the UI).
"""
from __future__ import annotations

import json
import math
import random
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from pydantic import ValidationError

from app.engine.backtest import (TRUE_WARNING_HORIZON_S, History, RuleInvalid, _as_specs, _replay, _spec_key,
                                 backtest, load_history)
from app.engine.rules import Ev, _check_match, compile_match, describe, describe_match, numeric_checks, validate
from app.models import SEVERITIES, Match
from app.sim.catalog import CUSTOMERS, HOST_ROLES, SOURCES, TEMPLATES, build_event

MAX_OUTPUT_CHARS = 2000
LOCAL_OFFSET_H = 1                         # the desk works in CET, like the simulator
TICKET_SOURCE, TICKET_CHECK = "servicenow", "incident"
NEAR_TICKET_BEFORE_MIN = 180               # an alert "near a ticket": up to 3 h before it ...
NEAR_TICKET_AFTER_MIN = 60                 # ... or up to 1 h after it, same customer
PRECURSOR_HORIZON_MIN = TRUE_WARNING_HORIZON_S // 60   # same horizon as the backtest
FORBIDDEN_KEYS = frozenset({"truth", "role", "pattern", "scenario_id", "incident_id"})
FORBIDDEN_TEXT = re.compile(r"\b(noise|tight|group|orphan|scenario):[a-z_]+")


class ToolError(ValueError):
    """A bad tool argument; the message goes back to the model as {"ok": false, "error": ...}."""


class TruthLeak(RuntimeError):
    pass


# =============================================================== index

def _sig(e: Ev) -> tuple[str, str]:
    return (e.source, e.check)


def sig_label(sig: tuple[str, str]) -> str:
    """Compact id for the model: source/check."""
    return f"{sig[0]}/{sig[1]}"


def sig_name(sig: tuple[str, str]) -> str:
    """Readable name for the work log: Vendor “check”."""
    return f"{SOURCES[sig[0]].vendor if sig[0] in SOURCES else sig[0]} “{sig[1]}”"


def _local_hour(t: float) -> int:
    return datetime.fromtimestamp(t + LOCAL_OFFSET_H * 3600, tz=timezone.utc).hour


def _fmt_ts(t: float) -> str:
    """Local (CET) 'MM-DD HH:MM'."""
    return datetime.fromtimestamp(t + LOCAL_OFFSET_H * 3600, tz=timezone.utc).strftime("%m-%d %H:%M")


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 1) if xs else None


def _quantile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return math.nan
    k = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo), 1)


@dataclass
class View:
    """The alerts operators see under one active rule set, indexed for the tools."""

    alerts: list[Ev]                                            # firing, time order
    by_cust: dict[str, list[Ev]] = field(default_factory=dict)
    times: dict[str, list[float]] = field(default_factory=dict)
    by_sig: dict[tuple[str, str], list[Ev]] = field(default_factory=dict)


class HistoryIndex:
    """Truth-free index over a History. Only `hist.events` is read here; `hist.truth`,
    `hist.incidents` and `hist.root_t` are never touched (backtest is the one exception, via the
    engine)."""

    def __init__(self, hist: History):
        self.hist = hist
        evs = hist.events
        self.t0 = evs[0].t if evs else 0.0
        self.t1 = evs[-1].t if evs else 0.0
        self.span_min = max(1.0, (self.t1 - self.t0) / 60)
        self.days = max(1, round(self.span_min / 1440))
        # minutes each firing stayed open (FIFO pairing with its resolve, as the engine does)
        self.duration: dict[str, float] = {}
        open_: dict[tuple, deque] = {}
        for e in evs:
            if e.status == "firing":
                open_.setdefault(e.key, deque()).append(e)
            else:
                dq = open_.get(e.key)
                if dq:
                    f = dq.popleft()
                    self.duration[f.id] = (e.t - f.t) / 60
        firing = [e for e in evs if e.status == "firing"]
        self.tickets = [e for e in firing if e.source == TICKET_SOURCE and e.check == TICKET_CHECK]
        self.ticket_times: dict[str, list[float]] = defaultdict(list)
        for e in self.tickets:
            self.ticket_times[e.customer].append(e.t)
        self._views: dict[str, View] = {}

    # ------------------------------------------------------------ views
    def view(self, active: Optional[Iterable[Any]]) -> View:
        specs = _as_specs(active or [])
        key = _spec_key(specs)
        v = self._views.get(key)
        if v is not None:
            return v
        base = self.hist._baseline.get(key)
        if base is None:
            base = _replay(self.hist, specs)
            self.hist._baseline[key] = base
        alerts = [e for e in self.hist.events if e.status == "firing" and e.id in base.passed]
        v = View(alerts)
        for e in alerts:
            v.by_cust.setdefault(e.customer, []).append(e)
            v.by_sig.setdefault(_sig(e), []).append(e)
        v.times = {c: [e.t for e in lst] for c, lst in v.by_cust.items()}
        if len(self._views) > 8:
            self._views.clear()
        self._views[key] = v
        return v

    # ------------------------------------------------------------ tickets
    def near_ticket(self, e: Ev) -> bool:
        ts = self.ticket_times.get(e.customer)
        if not ts:
            return False
        i = bisect_left(ts, e.t - NEAR_TICKET_AFTER_MIN * 60)
        return i < len(ts) and ts[i] <= e.t + NEAR_TICKET_BEFORE_MIN * 60

    def chance_near_ticket(self) -> float:
        """Share of customer-time inside a near-ticket window: the level noise reaches by chance."""
        win = NEAR_TICKET_BEFORE_MIN + NEAR_TICKET_AFTER_MIN
        covered = sum(min(len(ts) * win, self.span_min) for ts in self.ticket_times.values())
        return _pct(covered, self.span_min * max(1, len(CUSTOMERS)))


_INDEX: dict[int, HistoryIndex] = {}


def get_index(hist: Optional[History] = None) -> HistoryIndex:
    h = hist or load_history()
    idx = _INDEX.get(id(h))
    if idx is None or idx.hist is not h:
        idx = _INDEX[id(h)] = HistoryIndex(h)
    return idx


# =============================================================== context and helpers

@dataclass
class ToolContext:
    """What one agent run works against: the active rules at run start and the history."""

    active: list[Any]
    hist: Optional[History] = None

    @property
    def index(self) -> HistoryIndex:
        return get_index(self.hist)


def _example_title(e: Ev) -> str:
    return e.base_title


def _template(sig: tuple[str, str]) -> str:
    tpl = TEMPLATES.get(sig)
    return tpl.title.replace("{host}", "<host>") if tpl else ""


def parse_match(raw: Any, what: str = "match") -> tuple[Match, Callable[[Ev], bool]]:
    if not isinstance(raw, dict) or not raw:
        raise ToolError(f"{what} must be an object with at least one of: source, customer, host_glob, "
                        f"title_contains, severity_in")
    try:
        m = Match.model_validate(raw)
    except ValidationError as exc:
        errs = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            if err.get("type") == "extra_forbidden":
                errs.append(f"unknown key '{loc}'; allowed: source, customer, host_glob, title_contains, severity_in")
            elif "severity" in loc:
                errs.append(f"{loc}: severity must be one of {', '.join(SEVERITIES)}")
            else:
                errs.append(f"{loc}: {err.get('msg', 'invalid')}")
        raise ToolError("; ".join(errs)) from None
    errs = [x for x in _check_match(m, what) if "matches too much" not in x]
    if errs:
        raise ToolError("; ".join(errs))
    if not any(v not in (None, [], "") for v in m.model_dump().values()):
        raise ToolError(f"{what} needs at least one field")
    return m, compile_match(m)


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be a whole number between {lo} and {hi}") from None
    if not lo <= v <= hi:
        raise ToolError(f"{key} must be between {lo} and {hi}")
    return v


def _even_sample(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def assert_clean(obj: Any) -> None:
    """No truth-shaped key or ground-truth label anywhere in a tool output."""
    if isinstance(obj, dict):
        bad = FORBIDDEN_KEYS & set(obj)
        if bad:
            raise TruthLeak(f"tool output has key(s) {sorted(bad)}")
        for v in obj.values():
            assert_clean(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            assert_clean(v)
    elif isinstance(obj, str) and FORBIDDEN_TEXT.search(obj):
        raise TruthLeak("tool output contains a ground-truth label")


def cap_output(out: dict, limit: int = MAX_OUTPUT_CHARS) -> dict:
    """Drop rows from the end of the longest list until the JSON fits in `limit` characters."""
    def size(o: dict) -> int:
        return len(json.dumps(o, separators=(",", ":"), ensure_ascii=False))

    if size(out) <= limit:
        return out
    out = dict(out)
    out["truncated"] = True
    while size(out) > limit:
        lists = [(k, v) for k, v in out.items() if isinstance(v, list) and v]
        if not lists:
            break
        k, v = max(lists, key=lambda kv: len(json.dumps(kv[1], ensure_ascii=False)))
        out[k] = v[:-1]
    if size(out) > limit:                       # a single huge string: clip it
        for k, v in out.items():
            if isinstance(v, str) and len(v) > 200:
                out[k] = v[:200] + "…"
    return out


def dumps(out: dict) -> str:
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def _n(x: int) -> str:
    return f"{x:,}"


# =============================================================== tools

def tool_top_alerts(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    group_by = str(args.get("group_by") or "signature")
    if group_by not in ("signature", "host", "source", "customer"):
        raise ToolError("group_by must be one of: signature, host, source, customer")
    limit = _int(args, "limit", 12, 1, 25)
    sev = args.get("severity_in")
    if sev is not None:
        if not isinstance(sev, list) or not sev or any(s not in SEVERITIES for s in sev):
            raise ToolError(f"severity_in must be a non-empty list of: {', '.join(SEVERITIES)}")
    src = args.get("source")
    if src is not None and src not in SOURCES:
        raise ToolError(f"source '{src}' is not a known source")
    idx = ctx.index
    alerts = idx.view(ctx.active).alerts
    total = len(alerts)
    sel = [e for e in alerts if (sev is None or e.severity in sev) and (src is None or e.source == src)]
    rows: list[str] = []
    if group_by == "signature":
        groups: dict[tuple, list[Ev]] = defaultdict(list)
        for e in sel:
            groups[_sig(e)].append(e)
        for sig, evs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]:
            hosts = Counter(e.host for e in evs)
            top3 = hosts.most_common(3)
            top3_pct = _pct(sum(n for _, n in top3), len(evs))
            durs = [idx.duration[e.id] for e in evs if e.id in idx.duration]
            sevs = "/".join(s for s in SEVERITIES if any(e.severity == s for e in evs))
            near = _pct(sum(1 for e in evs if idx.near_ticket(e)), len(evs))
            dur = f"open ~{_median(durs):.0f}m" if durs else "never resolves"
            rows.append(f"{sig_label(sig)} | {len(evs)} {_pct(len(evs), total)}% | {sevs} | "
                        f"\"{_example_title(evs[0])[:64]}\" | {len(hosts)} hosts, top 3 = {top3_pct:.0f}% "
                        f"({', '.join(h for h, _ in top3)}) | {dur} | near ticket {near:.0f}%")
    else:
        keyf = {"host": lambda e: e.host, "source": lambda e: e.source, "customer": lambda e: e.customer}[group_by]
        groups2: dict[str, list[Ev]] = defaultdict(list)
        for e in sel:
            groups2[keyf(e)].append(e)
        for k, evs in sorted(groups2.items(), key=lambda kv: -len(kv[1]))[:limit]:
            sigs = Counter(_sig(e) for e in evs).most_common(2)
            rows.append(f"{k} | {_n(len(evs))} ({_pct(len(evs), total)}%) | mostly "
                        + ", ".join(f"{sig_label(s)} {n}" for s, n in sigs))
    out = {"ok": True, "alerts_total": total, "selected": len(sel), "days": idx.days, "group_by": group_by,
           "chance_near_ticket_pct": idx.chance_near_ticket(), "rows": rows}
    if group_by == "signature" and rows:
        top_sig, top_n = max(Counter(_sig(e) for e in sel).items(), key=lambda kv: kv[1])
        line = (f"Counted the alerts that reach operators: {_n(total)} in {idx.days} days; biggest is "
                f"{SOURCES[top_sig[0]].vendor} “{top_sig[1]}” with {_n(top_n)} ({_pct(top_n, total):g}%)")
    else:
        line = f"Counted the alerts by {group_by}: {_n(len(sel))} of {_n(total)}"
    return out, line


def tool_incident_tickets(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    idx = ctx.index
    groups: dict[str, list[Ev]] = defaultdict(list)
    for e in idx.tickets:
        groups[e.base_title].append(e)
    rows = []
    for title, evs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        prio = evs[0].native_severity
        whens = ", ".join(f"{e.customer} {_fmt_ts(e.t)}" for e in evs)
        rows.append(f"{prio} | \"{title}\" | {len(evs)}x | {whens}")
    out = {"ok": True, "tickets_total": len(idx.tickets), "days": idx.days,
           "note": "ServiceNow incident tickets in the history (priority | short description | count | customer and "
                   "local time). Match them with {\"source\": \"servicenow\", \"title_contains\": \"...\"}.",
           "rows": rows}
    return out, f"Read the {len(idx.tickets)} incident tickets of the month ({len(groups)} kinds)"


_BUCKETS = ("hour_of_day", "weekday", "day")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def tool_timeline(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    m, match = parse_match(args.get("match"))
    bucket = str(args.get("bucket") or "hour_of_day")
    if bucket not in _BUCKETS:
        raise ToolError(f"bucket must be one of: {', '.join(_BUCKETS)}")
    idx = ctx.index
    evs = [e for e in idx.view(ctx.active).alerts if match(e)]
    if bucket == "hour_of_day":
        c = Counter(_local_hour(e.t) for e in evs)
        counts = [c.get(h, 0) for h in range(24)]
        labels = [f"{h:02d}" for h in range(24)]
    elif bucket == "weekday":
        c = Counter(datetime.fromtimestamp(e.t + LOCAL_OFFSET_H * 3600, tz=timezone.utc).weekday() for e in evs)
        counts = [c.get(d, 0) for d in range(7)]
        labels = list(_WEEKDAYS)
    else:
        c = Counter(int((e.t - idx.t0) // 86400) for e in evs)
        counts = [c.get(d, 0) for d in range(idx.days)]
        labels = [f"d{d + 1}" for d in range(idx.days)]
    out = {"ok": True, "matched": len(evs), "bucket": bucket,
           "counts": " ".join(f"{lab}:{n}" for lab, n in zip(labels, counts))}
    if evs:
        peak = max(range(len(counts)), key=lambda i: counts[i])
        nonzero = sum(1 for n in counts if n)
        shape = ("spread evenly" if nonzero >= 0.8 * len(counts) and max(counts) < 2.5 * (len(evs) / len(counts))
                 else f"peaks at {labels[peak]} ({_pct(counts[peak], len(evs))}%)")
        out["shape"] = shape
        line = f"Timeline of {describe_match(m)} by {bucket.replace('_', ' ')}: {_n(len(evs))} alerts, {shape}"
    else:
        line = f"Timeline of {describe_match(m)}: no alerts"
    return out, line


def tool_co_occurrence(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    m, match = parse_match(args.get("match"))
    w = _int(args, "within_min", 5, 1, 120)
    limit = _int(args, "limit", 8, 1, 12)
    idx = ctx.index
    v = idx.view(ctx.active)
    anchors_all = [e for e in v.alerts if match(e)]
    anchors = _even_sample(anchors_all, 3000)
    own = {_sig(e) for e in anchors}
    found: Counter = Counter()
    gaps: dict[tuple, list[float]] = defaultdict(list)
    same_host: Counter = Counter()
    example: dict[tuple, Ev] = {}
    expected: Counter = Counter()
    per_cust_sig = Counter((e.customer, _sig(e)) for e in v.alerts)
    anchors_per_cust = Counter(e.customer for e in anchors)
    for a in anchors:
        lst, ts = v.by_cust[a.customer], v.times[a.customer]
        lo, hi = bisect_left(ts, a.t - w * 60), bisect_right(ts, a.t + w * 60)
        best: dict[tuple, Ev] = {}
        for e in lst[lo:hi]:
            s = _sig(e)
            if s in own or e.id == a.id:
                continue
            b = best.get(s)
            if b is None or abs(e.t - a.t) < abs(b.t - a.t):
                best[s] = e
        for s, e in best.items():
            found[s] += 1
            gaps[s].append((e.t - a.t) / 60)
            same_host[s] += e.host == a.host
            example.setdefault(s, e)
    for (cust, s), n in per_cust_sig.items():
        if s in found:
            expected[s] += anchors_per_cust.get(cust, 0) * (1 - math.exp(-n * 2 * w / idx.span_min))
    n_a = len(anchors)
    rows = []
    ranked_rows: list[tuple[str, str]] = []
    ranked = sorted(found, key=lambda s: (found[s] / max(expected[s], 1e-9) >= 3, found[s]), reverse=True)
    for s in ranked:
        if found[s] < max(2, 0.05 * n_a):
            continue
        lift = found[s] / max(expected[s], 1e-9)
        e = example[s]
        ranked_rows.append(s)
        rows.append(f"{sig_label(s)} | with {found[s]} of {n_a} ({_pct(found[s], n_a)}%) | lift {lift:.0f} | "
                    f"median gap {_median(gaps[s]):+g} min | same host {_pct(same_host[s], found[s])}% | "
                    f"e.g. \"{e.base_title}\" on {e.host}")
        if len(rows) >= limit:
            break
    out = {"ok": True, "anchors": len(anchors_all), "within_min": w,
           "note": "lift = how many times more often than chance; gap = minutes after the anchor alert", "rows": rows}
    if n_a > len(anchors):
        out["sampled_anchors"] = n_a
    if rows:
        top = rows[0].split(" | ")
        line = (f"Looked for alerts that fire together with {describe_match(m)}: {sig_name(ranked_rows[0])} "
                f"comes {top[1]}")
    else:
        line = f"Looked for alerts that fire together with {describe_match(m)}: nothing stands out"
    return out, line


def _simulate_precursor(alerts: list[Ev], count: int, within_min: int, anchor_times: dict[str, list[float]],
                        min_lead_min: int = 0) -> tuple[int, int, list[float]]:
    """What a precursor rule on these alerts would have done, judged against the anchors (e.g.
    incident tickets): (warnings, warnings followed by an anchor at least `min_lead_min` and at
    most the backtest horizon later, leads in minutes)."""
    recent: dict[str, deque] = defaultdict(deque)
    quiet: dict[str, float] = {}
    fired = ok = 0
    leads: list[float] = []
    w = within_min * 60
    for e in alerts:
        c = e.customer
        if e.t < quiet.get(c, -math.inf):
            continue
        dq = recent[c]
        dq.append(e.t)
        while dq and dq[0] < e.t - w:
            dq.popleft()
        if len(dq) < count:
            continue
        dq.clear()
        quiet[c] = e.t + w
        fired += 1
        ts = anchor_times.get(c, [])
        i = bisect_left(ts, e.t + min_lead_min * 60)
        if i < len(ts) and ts[i] <= e.t + PRECURSOR_HORIZON_MIN * 60:
            ok += 1
            leads.append((ts[i] - e.t) / 60)
    return fired, ok, leads


def _best_precursor(alerts: list[Ev], anchor_times: dict[str, list[float]], within_min: int,
                    min_lead_min: int = 0) -> Optional[tuple[int, int, int, Optional[float]]]:
    """The count (1-3) that works best as a precursor: (count, warnings, followed, median lead)."""
    best = None
    for c in (1, 2, 3):
        fired, ok, leads = _simulate_precursor(alerts, c, within_min, anchor_times, min_lead_min)
        if not fired:
            continue
        score = (ok > 0, ok / fired, ok)
        if best is None or score > best[0]:
            best = (score, (c, fired, ok, _median(leads)))
    return best[1] if best else None


def tool_preceded_by(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    m, match = parse_match(args.get("match"))
    lb = _int(args, "lookback_min", 360, 5, 2880)
    min_lead = _int(args, "min_lead_min", 60, 0, 1440)
    if min_lead >= lb:
        raise ToolError("min_lead_min must be smaller than lookback_min")
    limit = _int(args, "limit", 6, 1, 12)
    idx = ctx.index
    v = idx.view(ctx.active)
    matched = [e for e in v.alerts if match(e)]
    # one anchor per episode: skip anchors within the lookback of the previous one (same customer)
    kept: list[Ev] = []
    last: dict[str, float] = {}
    for e in matched:
        if e.t - last.get(e.customer, -math.inf) > lb * 60:
            kept.append(e)
            last[e.customer] = e.t
    own = {_sig(e) for e in matched}
    hit: Counter = Counter()
    leads: dict[tuple, list[float]] = defaultdict(list)
    counts: dict[tuple, list[int]] = defaultdict(list)
    example: dict[tuple, Ev] = {}
    for a in kept:
        lst, ts = v.by_cust[a.customer], v.times[a.customer]
        lo, hi = bisect_left(ts, a.t - lb * 60), bisect_right(ts, a.t - min_lead * 60)
        first: dict[tuple, Ev] = {}
        cnt: Counter = Counter()
        for e in lst[lo:hi]:
            s = _sig(e)
            if s in own:
                continue
            cnt[s] += 1
            first.setdefault(s, e)
        for s, e in first.items():
            hit[s] += 1
            leads[s].append((a.t - e.t) / 60)
            counts[s].append(cnt[s])
            example.setdefault(s, e)
    anchor_times: dict[str, list[float]] = defaultdict(list)
    for e in matched:
        anchor_times[e.customer].append(e.t)

    def followed(e: Ev) -> bool:
        ts = anchor_times.get(e.customer)
        if not ts:
            return False
        i = bisect_right(ts, e.t)
        return i < len(ts) and ts[i] <= e.t + lb * 60

    covered = sum(min(len(ts) * lb, idx.span_min) for ts in anchor_times.values())
    chance = _pct(covered, idx.span_min * max(1, len(CUSTOMERS)))
    n_k = len(kept)
    within = min(lb, 1440)
    scored = []
    for s in hit:
        if hit[s] < max(2 if n_k >= 4 else 1, 0.25 * n_k):
            continue
        allv = v.by_sig.get(s, [])
        fp = _pct(sum(1 for e in allv if followed(e)), len(allv))
        sim = _best_precursor(allv, anchor_times, within, min_lead)
        sim_ratio = sim[2] / sim[1] if sim else 0.0
        scored.append((round(hit[s] / max(n_k, 1) * fp, 3), sim_ratio, sig_label(s), s, fp, len(allv), sim))
    scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
    rows = []
    for _, _, _, s, fp, tot, sim in scored[:limit]:
        e = example[s]
        as_rule = ""
        if sim:
            c, fired, ok, lead = sim
            as_rule = (f" | as a rule, count {c} within {within} min: {fired} warnings, {ok} followed by one "
                       f"{min_lead} min to {PRECURSOR_HORIZON_MIN // 60} h later"
                       + (f", lead ~{lead:.0f} min" if lead is not None else ""))
        rows.append(f"{sig_label(s)} | before {hit[s]} of {n_k} | median lead {_median(leads[s]):g} min | "
                    f"typically {int(statistics.median(counts[s]))} in the window | {fp}% of its {_n(tot)} alerts "
                    f"were followed by one{as_rule} | e.g. \"{e.base_title[:70]}\"")
    out = {"ok": True, "anchors": n_k, "lookback_min": lb, "min_lead_min": min_lead, "chance_followed_pct": chance,
           "note": f"alerts seen {min_lead} to {lb} min before each anchor (one anchor per episode); 'followed' = an "
                   "anchor came within the lookback at the same customer", "rows": rows}
    if rows:
        top = rows[0].split(" | ")
        line = (f"Looked for early signs before {describe_match(m)}: {sig_name(scored[0][3])} came {top[1]}, "
                f"{top[2].replace('median lead ', '')} ahead")
    else:
        line = f"Looked for early signs before {describe_match(m)}: no consistent sign"
    return out, line


def tool_value_distribution(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    src, chk = args.get("source"), args.get("check")
    if src not in SOURCES:
        raise ToolError(f"source '{src}' is not a known source")
    if (src, chk) not in TEMPLATES:
        raise ToolError(f"check '{chk}' is not a check of {src}; known: "
                        + ", ".join(c for (s, c) in TEMPLATES if s == src))
    tpl = TEMPLATES[(src, chk)]
    if tpl.threshold is None:
        nums = numeric_checks(src)
        raise ToolError(f"check '{chk}' of {src} carries no numeric value; numeric checks: {', '.join(nums) or 'none'}")
    idx = ctx.index
    evs = [e for e in idx.view(ctx.active).by_sig.get((src, chk), []) if e.value is not None]
    vals = sorted(e.value for e in evs)
    unit = tpl.unit.strip()
    out: dict[str, Any] = {"ok": True, "source": src, "check": chk, "unit": unit, "alerts": len(evs),
                           "current_threshold": f"> {tpl.threshold:g}"}
    if not vals:
        return out, f"Read the values of {SOURCES[src].vendor} “{chk}”: no alerts"
    out["percentiles"] = {f"p{int(q * 100)}": _quantile(vals, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)}
    out["max"] = vals[-1]
    durs = [idx.duration[e.id] for e in evs if e.id in idx.duration]
    if durs:
        out["open_minutes"] = {"median": _median(durs), "share_cleared_within_5_min_pct": _pct(sum(d <= 5 for d in durs), len(durs))}
    near = sorted((e.value for e in evs if idx.near_ticket(e)), reverse=True)
    out["values_near_a_ticket"] = {"count": len(near), "highest": near[:8]}
    cands = sorted({_quantile(vals, 0.5), _quantile(vals, 0.9), round(tpl.threshold + 10, 1), round(tpl.threshold + 5, 1)})
    out["alerts_kept_if_threshold"] = {f"> {c:g}": sum(1 for x in vals if x > c) for c in cands}
    line = (f"Read the values of {SOURCES[src].vendor} “{chk}”: median {out['percentiles']['p50']:g}{unit}, "
            f"90th percentile {out['percentiles']['p90']:g}{unit}, threshold {tpl.threshold:g}{unit}")
    if durs:
        line += f", {out['open_minutes']['share_cleared_within_5_min_pct']:g}% clear within 5 min"
    return out, line


def backtest_summary(bt: Any, rule: Any) -> dict:
    """The model-facing view of one backtest: aggregate numbers only."""
    d: dict[str, Any] = {
        "ok": True, "rule_says": describe(rule),
        "alerts_before": bt.alerts_before, "alerts_after": bt.alerts_after,
        "alerts_saved": bt.alerts_saved, "saved_pct": bt.saved_pct,
        "incidents_still_visible": f"{bt.incidents_visible} of {bt.incidents_total}",
        "incidents_hidden": bt.incidents_hidden,
    }
    if rule.kind == "precursor":
        d.update(early_warnings=bt.precursor_fired, warnings_followed_by_incident=bt.precursor_true,
                 false_alarms=bt.precursor_false, median_lead_min=bt.median_lead_min)
        if bt.precursor_fired == 0:
            d["hint"] = "it never fired: lower count, widen within_min or loosen the match"
        elif bt.precursor_true == 0 or bt.precursor_false > bt.precursor_true:
            d["hint"] = ("mostly false alarms: raise count, narrow the match to the exact alert wording, or pick "
                         "a sign that comes closer before the failure")
        elif (bt.median_lead_min or 0) < 10:
            d["hint"] = "it fires with the failure itself, not before it: pick an earlier sign"
    d["samples"] = bt.samples[:3]
    return d


def tool_backtest(args: dict, ctx: ToolContext) -> tuple[dict, str]:
    raw = args.get("rule", args if "kind" in args else None)
    title = str(args.get("title") or "").strip()
    rule, errs = validate(raw)
    if errs:
        out = {"ok": False, "errors": errs[:4], "hint": "fix the rule and backtest again"}
        return out, f"Backtest refused the rule: {errs[0]}"
    try:
        bt = backtest(rule, ctx.active, history=ctx.hist)
    except RuleInvalid as e:                           # pragma: no cover - validate() ran first
        return {"ok": False, "errors": e.errors[:4]}, f"Backtest refused the rule: {e.errors[0]}"
    out = backtest_summary(bt, rule)
    name = title or describe(rule)
    if rule.kind == "precursor":
        lead = f", median lead {bt.median_lead_min / 60:.1f} h" if bt.median_lead_min else ""
        line = (f"Backtested: {name} → {bt.precursor_fired} early warnings, {bt.precursor_true} right, "
                f"{bt.precursor_false} false alarms{lead}")
    else:
        line = (f"Backtested: {name} → {_n(bt.alerts_saved)} alerts saved ({bt.saved_pct:g}%), "
                f"{bt.incidents_hidden} incident{'s' if bt.incidents_hidden != 1 else ''} hidden")
    return out, line


TOOLS: dict[str, Callable[[dict, ToolContext], tuple[dict, str]]] = {
    "top_alerts": tool_top_alerts,
    "incident_tickets": tool_incident_tickets,
    "timeline": tool_timeline,
    "co_occurrence": tool_co_occurrence,
    "preceded_by": tool_preceded_by,
    "value_distribution": tool_value_distribution,
    "backtest": tool_backtest,
}
SUBMIT = "submit_proposals"


def run_tool(name: str, args: Any, ctx: ToolContext) -> tuple[dict, str]:
    """Run one tool. Bad arguments come back as {"ok": false, "error": ...}; never raises for them."""
    fn = TOOLS.get(name)
    if fn is None:
        out, line = {"ok": False, "error": f"unknown tool '{name}'; tools: {', '.join([*TOOLS, SUBMIT])}"}, \
            f"Tried an unknown tool “{name}”"
    elif not isinstance(args, dict):
        out, line = {"ok": False, "error": "arguments must be a JSON object"}, f"Sent unreadable arguments to {name}"
    else:
        try:
            out, line = fn(args, ctx)
        except ToolError as e:
            out, line = {"ok": False, "error": str(e)}, f"{name} refused the arguments: {e}"
    out = cap_output(out)
    assert_clean(out)
    return out, line


def sample_alert(rule: Any, hist: Optional[History] = None) -> Optional[dict]:
    """One alert the rule is about, as its tool sends it (title, severity word, vendor `raw`
    block), rebuilt from the catalog for the native-syntax draft. No truth."""
    idx = get_index(hist)
    if rule.kind == "retune":
        match = lambda e: e.source == rule.source and e.check == rule.check   # noqa: E731
    elif rule.kind == "correlate":
        match = compile_match(rule.members[0])
    else:
        match = compile_match(rule.match)
    e = next((e for e in idx.hist.events if e.status == "firing" and match(e)), None)
    if e is None or e.source not in SOURCES or (e.source, e.check) not in TEMPLATES:
        return None
    cust = CUSTOMERS.get(e.customer)
    if cust is None:
        return None
    role = next((r for r in HOST_ROLES if e.host in cust.hosts(r)), "app")
    ev = build_event(cust, e.source, e.check, e.host, role, e.t, random.Random(0), value=e.value,
                     threshold=e.threshold, native=e.native_severity)
    return {"title": e.title, "native_severity": e.native_severity, "raw": ev["raw"]}


# =============================================================== schemas (OpenAI function tools)

_MATCH_SCHEMA = {
    "type": "object",
    "description": "Alert selector; all given fields must match.",
    "properties": {
        "source": {"type": "string", "description": "source id, e.g. checkmk"},
        "customer": {"type": "string"},
        "host_glob": {"type": "string", "description": "shell-style glob on the host name, e.g. *-test-vm-*"},
        "title_contains": {"type": "string", "description": "case-insensitive substring of the alert title"},
        "severity_in": {"type": "array", "items": {"type": "string", "enum": list(SEVERITIES)}},
    },
    "additionalProperties": False,
}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required, "additionalProperties": False}}}


TOOL_SCHEMAS: list[dict] = [
    _fn("top_alerts", "The biggest groups of alerts that reach the operators today (after the active rules), "
        "with share, hosts, customers, how long they stay open and how often they sit near an incident ticket.",
        {"group_by": {"type": "string", "enum": ["signature", "host", "source", "customer"],
                      "description": "signature = source/check (default)"},
         "severity_in": {"type": "array", "items": {"type": "string", "enum": list(SEVERITIES)}},
         "source": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, []),
    _fn("incident_tickets", "The ServiceNow incident tickets of the month: which problems really needed people.",
        {}, []),
    _fn("timeline", "When matching alerts fire: counts per local hour of day, weekday or day of the month.",
        {"match": _MATCH_SCHEMA, "bucket": {"type": "string", "enum": list(_BUCKETS)}}, ["match"]),
    _fn("co_occurrence", "Which other alerts fire at the same customer within N minutes of the matching alerts, "
        "with a lift score against chance. Use it to find alerts that belong together.",
        {"match": _MATCH_SCHEMA, "within_min": {"type": "integer", "minimum": 1, "maximum": 120},
         "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, ["match"]),
    _fn("preceded_by", "Which alerts fire at the same customer in the lookback window BEFORE the matching alerts "
        "(e.g. before an incident ticket), and how often each of them is followed by one. Use it to find early "
        "warnings.",
        {"match": _MATCH_SCHEMA, "lookback_min": {"type": "integer", "minimum": 5, "maximum": 2880},
         "min_lead_min": {"type": "integer", "minimum": 0, "maximum": 1440,
                          "description": "ignore alerts closer than this to the anchor (default 60): a ticket "
                                         "comes 15-60 min after the failure starts, so closer alerts are the "
                                         "failure itself, not a warning"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, ["match"]),
    _fn("value_distribution", "Percentiles of the measured values of one numeric check, its current threshold, "
        "how fast the alerts clear, the values seen near incident tickets, and how many alerts other thresholds "
        "would keep.",
        {"source": {"type": "string"}, "check": {"type": "string"}}, ["source", "check"]),
    _fn("backtest", "Replay the 30 days with this rule added to the active rules. Returns alerts saved, incidents "
        "still visible / hidden and, for early-warning rules, how many warnings were right. Errors come back as "
        "short strings: fix and retry.",
        {"rule": {"type": "object", "description": "a rule in the DSL"},
         "title": {"type": "string", "description": "short name for the work log"}}, ["rule"]),
    _fn(SUBMIT, "Hand in your final proposals (at most 5). This ends your work.",
        {"proposals": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "at most 8 words"},
                "kind": {"type": "string", "enum": ["suppress", "precursor", "retune", "correlate"]},
                "reasoning": {"type": "string", "description": "two or three plain sentences for an operator"},
                "rule": {"type": "object"},
            },
            "required": ["title", "reasoning", "rule"],
        }}}, ["proposals"]),
]


# =============================================================== reference text for the prompt

def catalog_reference() -> str:
    """Valid sources, their severity words and checks, generated from the catalog."""
    lines = []
    for sid, s in SOURCES.items():
        sev = ", ".join(f"{k}={v}" for k, v in s.severity_map.items() if k not in ("Resolved", "OK", "UP", "Up", "Clear"))
        checks = []
        for (src, chk), t in TEMPLATES.items():
            if src != sid:
                continue
            checks.append(f"{chk} (numeric, threshold {t.threshold:g}{t.unit.strip()})" if t.threshold is not None else chk)
        lines.append(f"- {sid} ({s.vendor}): checks {', '.join(checks)}; severities {sev}")
    custs = "; ".join(f"{c.id} = {c.short} (hosts {c.prefix}-*)" for c in CUSTOMERS.values())
    return "\n".join(lines) + f"\nCustomers: {custs}"
