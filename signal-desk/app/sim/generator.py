"""Seeded, deterministic alert generation: baseline noise, patch nights and scenario
expansion. The same code feeds the 30-day history (hour buckets) and the live stream
(minute buckets), so what the rule engineer learns from history is what the live demo shows.

Every random draw comes from a `random.Random` seeded with a string that names what is being
drawn (seed, customer, stream, bucket). String seeds are hashed with SHA-512 by CPython, so
the output does not depend on PYTHONHASHSEED or on the order streams are listed.

Designed patterns a rule-proposing agent can find (labelled in `truth.pattern`):
- noise:*   recurring sources nobody acts on (NTP offset flaps, a decommissioned host's
            heartbeat, nightly "Success" notifications, PRTG "Unusual" traffic ...)
- tight:*   thresholds set too low: values sit just above the threshold for 1-5 minutes
            (Zabbix CPU > 80 %, Checkmk /var > 80 %, Azure CPU > 70 %, CPU throttling > 25 %)
            while real incidents push the same checks far higher for 20+ minutes
- group:*   alerts that always fire together within a couple of minutes
- orphan:*  precursor runs that lead to nothing (false alarms for precursor rules)
- patch_night  bursts of reboot alerts in each customer's patch window
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.sim.catalog import CUSTOMERS, Customer, build_event
from app.sim.scenarios import SCENARIOS, Scenario

LOCAL_OFFSET_H = 1          # shaping uses CET (UTC+1); timestamps stay UTC


# ---------------------------------------------------------------- noise streams

@dataclass(frozen=True)
class Stream:
    name: str                               # truth.pattern
    source: str
    check: str
    host_role: str
    rate: float                             # firings per host per hour before shaping
    profile: str = "flat"                   # flat | business | night | nightly
    hosts: Optional[int] = None             # first n hosts of the role (None = all)
    host_names: tuple[str, ...] = ()        # explicit host names ("{p}" = customer prefix)
    resolve: Optional[tuple[float, float]] = None
    value: Optional[tuple[float, float]] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Member:
    source: str
    check: str
    host_role: str
    delay: tuple[float, float] = (0.0, 2.0)
    resolve: Optional[tuple[float, float]] = (3.0, 8.0)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Group:
    name: str
    anchor_role: str
    rate: float                             # occurrences per customer per hour before shaping
    profile: str
    members: tuple[Member, ...]


STREAMS: tuple[Stream, ...] = (
    # recurring noise
    Stream("noise:checkmk_ntp", "checkmk", "ntp_time", "win", 0.22, resolve=(2, 9), value=(151, 190)),
    Stream("noise:prtg_traffic", "prtg", "traffic_unusual", "branch", 0.16, "business", resolve=(10, 30)),
    Stream("noise:zabbix_test_vm", "zabbix", "agent_unavailable", "app", 1.4, host_names=("{p}-test-vm-03",),
           resolve=(1, 4)),
    Stream("noise:scom_stale_host", "scom", "health_service_unavailable", "win", 1.1, host_names=("{p}-win-old-07",),
           resolve=(2, 6)),
    Stream("noise:fortigate_admin", "fortigate", "admin_login", "fw", 0.35, "business"),
    Stream("noise:azure_advisor", "azure_monitor", "advisor", "app", 0.3, hosts=1),
    Stream("noise:veeam_success", "veeam", "job_success", "backup", 1.3, "nightly"),
    Stream("noise:controlm_ok", "controlm", "ended_ok", "batch", 1.2, "nightly"),
    Stream("noise:redgate_ple", "redgate", "ple_low", "sql", 0.12, "business"),
    Stream("noise:vcf_mem", "vcf_ops", "mem_contention", "esx", 0.08, "business"),
    Stream("noise:dt_response_blip", "dynatrace", "response_time", "web", 0.04, "business", resolve=(3, 10)),
    Stream("noise:k8s_hpa", "alertmanager", "KubeHpaMaxedOut", "k8s", 0.12, "business", resolve=(10, 30)),
    Stream("noise:sap_integration", "sap_alm", "integration_failed", "sap", 0.2, "business"),
    Stream("noise:splunk_logins", "splunk_es", "failed_logins", "dc", 0.2),
    Stream("noise:sentinel_signins", "sentinel", "failed_signins", "dc", 0.1),
    Stream("noise:sentinel_rare_op", "sentinel", "rare_operation", "app", 0.02, hosts=1),
    Stream("noise:defender_pua", "defender_xdr", "pua", "win", 0.012, "business"),
    Stream("noise:defender_ps", "defender_xdr", "suspicious_ps", "win", 0.006, "business"),
    Stream("noise:crowdstrike_pup", "crowdstrike", "pup", "win", 0.012, "business"),
    Stream("noise:solarwinds_resp", "solarwinds", "high_response", "branch", 0.06, "business", resolve=(5, 20)),
    Stream("noise:redgate_deadlock", "redgate", "deadlock", "sql", 0.02, "business"),
    Stream("noise:zabbix_swap", "zabbix", "swap_low", "app", 0.05, resolve=(20, 90)),
    Stream("noise:scom_disk", "scom", "logical_disk", "win", 0.02, resolve=(30, 120)),
    Stream("noise:vcf_datastore", "vcf_ops", "datastore_space", "esx", 0.01),
    Stream("noise:dt_low_disk", "dynatrace", "low_disk", "app", 0.01, value=(90.5, 93.0), resolve=(30, 90)),
    # thresholds set too tight: brief, shallow crossings
    Stream("tight:zabbix_cpu", "zabbix", "cpu_util", "app", 0.18, "business", resolve=(1, 4), value=(80.2, 87.5)),
    Stream("tight:zabbix_cpu", "zabbix", "cpu_util", "batch", 0.35, "night", resolve=(1, 4), value=(80.2, 87.5)),
    Stream("tight:checkmk_fs", "checkmk", "fs_var", "app", 0.09, resolve=(5, 25), value=(80.1, 83.5)),
    Stream("tight:azure_cpu", "azure_monitor", "cpu_pct", "app", 0.14, "business", resolve=(5, 10), value=(70.2, 77.0)),
    Stream("tight:k8s_throttling", "alertmanager", "CPUThrottlingHigh", "k8s", 0.25, "business", resolve=(5, 15),
           value=(25.5, 41.0)),
)

GROUPS: tuple[Group, ...] = (
    Group("group:nightly_snapshot", "backup", 0.25, "nightly", (
        Member("veeam", "job_warning", "backup", (0, 1), None),
        Member("netapp", "snap_reserve", "nas", (0, 2), (60, 180)),
    )),
    Group("group:branch_vpn_blip", "branch", 0.07, "business", (
        Member("fortigate", "vpn_down", "fw", (0, 0.5), (3, 8)),
        Member("prtg", "ping", "branch", (0.5, 1.5), (3, 8)),
        Member("solarwinds", "node_down", "branch", (1, 2), (3, 8)),
        Member("checkmk", "host_unreach", "branch", (1, 2.5), (3, 8)),
    )),
    Group("group:win_agent_restart", "win", 0.05, "flat", (
        Member("scom", "heartbeat_failure", "win", (0, 0.5), (2, 6)),
        Member("scom", "failed_connect", "win", (0.5, 1.5), (2, 6)),
        Member("defender_xdr", "sensor_stale", "win", (1, 3), None),
    )),
)


# ---------------------------------------------------------------- helpers

def parse_iso(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=LOCAL_OFFSET_H)


def shape(profile: str, ts: float) -> float:
    """Diurnal / weekly multiplier for a stream profile at simulated time ts."""
    t = _local(ts)
    h, weekend = t.hour, t.weekday() >= 5
    if profile == "business":
        if 8 <= h < 18:
            f = 1.9
        elif 6 <= h < 8 or 18 <= h < 22:
            f = 0.8
        else:
            f = 0.2
        return f * (0.35 if weekend else 1.0)
    if profile == "night":
        return 2.6 if 0 <= h < 5 else 0.15
    if profile == "nightly":
        return 1.0 if 1 <= h < 4 else 0.0
    return 1.0


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _rng(*parts: Any) -> random.Random:
    return random.Random(":".join(str(p) for p in parts))


def _truth(pattern: Optional[str], role: str = "noise", scenario_id: Optional[str] = None,
           incident_id: Optional[str] = None) -> dict:
    return {"scenario_id": scenario_id, "role": role, "incident_id": incident_id, "pattern": pattern}


def _fire_and_resolve(out: list, customer: Customer, source: str, check: str, host: str, role: str,
                      ts: float, rng: random.Random, truth: dict, *, value: Optional[float] = None,
                      threshold: Optional[float] = None, native: Optional[str] = None,
                      resolve_after_min: Optional[float] = None, extra: Optional[dict] = None,
                      resolve_ts: Optional[float] = None) -> None:
    ev = build_event(customer, source, check, host, role, ts, rng, value=value, threshold=threshold,
                     native=native, extra=extra, truth=truth)
    out.append(ev)
    if resolve_ts is None and resolve_after_min is not None:
        resolve_ts = ts + resolve_after_min * 60
    if resolve_ts is not None:
        out.append(build_event(customer, source, check, host, role, resolve_ts, rng, status="resolved",
                               value=value, threshold=threshold, native=native, nid=ev["_nid"],
                               picks=ev["_picks"], extra=extra, truth=dict(truth)))


def _stream_hosts(customer: Customer, s: Stream) -> list[str]:
    if s.host_names:
        return [h.replace("{p}", customer.prefix) for h in s.host_names]
    hosts = customer.hosts(s.host_role)
    return hosts[: s.hosts] if s.hosts else hosts


# ---------------------------------------------------------------- baseline noise

def _rate_factor(name: str, profile: str, ts: float, rate_mult: float,
                 mix: Optional[dict[str, float]]) -> float:
    """Shaping x multiplier. A pattern listed in `mix` (live stream only) runs flat over the day
    at its own multiplier, so the recurring noise rules can remove is a steady share at any hour."""
    if mix and name in mix:
        return float(mix[name])
    return shape(profile, ts) * rate_mult


def _bucket_count(rng: random.Random, lam: float, b: int, bucket_s: int, regular_phase: Optional[float]) -> int:
    """How many firings land in bucket b. History and unlisted live streams: Poisson. A live
    `mix` family (flat rate) instead fires on a steady rhythm: the running total floor(lam * k +
    phase) over bucket index k, so its count over any few minutes is within one of the expected
    value. That keeps the noise-drop figure after an accepted rule close to the rule's share
    instead of swinging with Poisson luck over a dozen simulated minutes."""
    if regular_phase is None:
        return poisson(rng, lam)
    k = b // bucket_s
    return math.floor(lam * (k + 1) + regular_phase) - math.floor(lam * k + regular_phase)


def noise_events(customer: Customer, start: float, end: float, bucket_s: int, seed: Any,
                 rate_mult: float = 1.0, mix: Optional[dict[str, float]] = None) -> list[dict]:
    """Baseline noise for one customer in [start, end), bucketed so that the draw for a given
    bucket is the same whether it is generated for the history or the live stream.

    `mix` (live only): {pattern: multiplier} for the recurring-noise families. They keep their
    titles, hosts, values and timing, only their rate changes, so a rule that backtests well on
    the history removes the same alerts live, just a bigger share of them."""
    out: list[dict] = []
    srcs = set(customer.sources)
    t0 = int(start // bucket_s * bucket_s)
    for s in STREAMS:
        if s.source not in srcs:
            continue
        hosts = _stream_hosts(customer, s)
        phase = (_rng(seed, customer.id, s.name, s.check, s.host_role, "phase").random()
                 if mix and s.name in mix else None)
        for b in range(t0, int(end), bucket_s):
            lam = s.rate * len(hosts) * _rate_factor(s.name, s.profile, b + bucket_s / 2, rate_mult, mix) * bucket_s / 3600
            if lam <= 0:
                continue
            rng = _rng(seed, customer.id, s.name, s.check, s.host_role, b, bucket_s)
            for _ in range(_bucket_count(rng, lam, b, bucket_s, phase)):
                ts = b + rng.random() * bucket_s
                if not (start <= ts < end):
                    continue
                host = rng.choice(hosts)
                value = round(rng.uniform(*s.value), 1) if s.value else None
                res = rng.uniform(*s.resolve) if s.resolve else None
                _fire_and_resolve(out, customer, s.source, s.check, host, s.host_role, ts, rng,
                                  _truth(s.name), value=value, resolve_after_min=res, extra=s.extra or None)
    for g in GROUPS:
        members = [m for m in g.members if m.source in srcs]
        if len(members) < 2:
            continue
        phase = _rng(seed, customer.id, g.name, "phase").random() if mix and g.name in mix else None
        for b in range(t0, int(end), bucket_s):
            lam = g.rate * _rate_factor(g.name, g.profile, b + bucket_s / 2, rate_mult, mix) * bucket_s / 3600
            if lam <= 0:
                continue
            rng = _rng(seed, customer.id, g.name, b, bucket_s)
            for _ in range(_bucket_count(rng, lam, b, bucket_s, phase)):
                ts = b + rng.random() * bucket_s
                if not (start <= ts < end):
                    continue
                anchor = rng.choice(customer.hosts(g.anchor_role))
                for m in members:
                    host = anchor if m.host_role == g.anchor_role else rng.choice(customer.hosts(m.host_role))
                    mts = ts + rng.uniform(*m.delay) * 60
                    res = rng.uniform(*m.resolve) if m.resolve else None
                    _fire_and_resolve(out, customer, m.source, m.check, host, m.host_role, mts, rng,
                                      _truth(g.name), resolve_after_min=res, extra=m.extra or None)
    return out


# ---------------------------------------------------------------- patch nights

def patch_windows(customer: Customer, start: float, end: float) -> list[tuple[float, float]]:
    """Patch windows 22:00-02:00 local on the customer's weekday, every second ISO week."""
    out = []
    day = datetime.fromtimestamp(start, tz=timezone.utc).replace(hour=0, minute=0, second=0)
    while day.timestamp() < end:
        local_day = day + timedelta(hours=LOCAL_OFFSET_H)
        if local_day.weekday() == customer.patch_weekday and local_day.isocalendar()[1] % 2 == customer.patch_week_parity:
            ws = day.timestamp() + (22 - LOCAL_OFFSET_H) * 3600
            if start <= ws and ws + 4 * 3600 <= end:
                out.append((ws, ws + 4 * 3600))
        day += timedelta(days=1)
    return out


def patch_night_events(customer: Customer, window: tuple[float, float], seed: Any) -> list[dict]:
    """Every Windows host reboots once in the window, and every tool notices."""
    out: list[dict] = []
    srcs = set(customer.sources)
    rng = _rng(seed, customer.id, "patch", int(window[0]))
    tr = _truth("patch_night")
    for role in ("win", "sql", "dc"):
        for host in customer.hosts(role):
            t = window[0] + rng.uniform(10, 200) * 60
            if "scom" in srcs:
                _fire_and_resolve(out, customer, "scom", "heartbeat_failure", host, role, t + 30, rng, dict(tr),
                                  resolve_after_min=rng.uniform(4, 9))
            if "checkmk" in srcs:
                _fire_and_resolve(out, customer, "checkmk", "host_down", host, role, t + 60, rng, dict(tr),
                                  resolve_after_min=rng.uniform(3, 7))
                _fire_and_resolve(out, customer, "checkmk", "ntp_time", host, role, t + rng.uniform(7, 10) * 60,
                                  rng, dict(tr), value=round(rng.uniform(160, 420), 1),
                                  resolve_after_min=rng.uniform(5, 15))
            if "zabbix" in srcs:
                _fire_and_resolve(out, customer, "zabbix", "agent_unavailable", host, role,
                                  t + rng.uniform(1, 3) * 60, rng, dict(tr), resolve_after_min=rng.uniform(3, 6))
                _fire_and_resolve(out, customer, "zabbix", "restarted", host, role, t + rng.uniform(6, 9) * 60,
                                  rng, dict(tr))
            if "prtg" in srcs and rng.random() < 0.6:
                _fire_and_resolve(out, customer, "prtg", "ping", host, role, t + 60, rng, dict(tr),
                                  resolve_after_min=rng.uniform(3, 6))
            if "defender_xdr" in srcs and rng.random() < 0.5:
                _fire_and_resolve(out, customer, "defender_xdr", "sensor_stale", host, role,
                                  t + rng.uniform(5, 20) * 60, rng, dict(tr))
            if "solarwinds" in srcs and rng.random() < 0.4:
                _fire_and_resolve(out, customer, "solarwinds", "node_down", host, role, t + 90, rng, dict(tr),
                                  resolve_after_min=rng.uniform(3, 6))
    return out


# ---------------------------------------------------------------- scenario expansion

def compress_fn(offsets: Iterable[float], span_min: float):
    """Piecewise-linear map onto exactly span_min minutes: the part before the root takes 40 %
    (if there is one), the part after it the rest. Order is preserved; short scripts stretch,
    long ones shrink, so every live scenario plays for about the same time."""
    offs = list(offsets)
    lo, hi = min(offs + [0.0]), max(offs + [0.0])
    pre_share = 0.4 if lo < 0 else 0.0
    if hi <= 0:
        pre_share = 1.0
    s_pre = pre_share * span_min / -lo if lo < 0 else 1.0
    s_post = (1 - pre_share) * span_min / hi if hi > 0 else 1.0
    return lambda x: x * s_pre if x < 0 else x * s_post


def plan_scenario(sc: Scenario, customer: Customer, rng: random.Random, *,
                  roles: Optional[set[str]] = None) -> list[tuple]:
    """Draw the concrete timeline of one occurrence: (fire_off, resolve_off|None, step, host, value, rep)."""
    picks: dict[str, list[str]] = {}
    plan = []
    for step in sc.steps:
        if roles and step.role not in roles:
            continue
        if step.host_role not in picks:
            hosts = customer.hosts(step.host_role)
            picks[step.host_role] = rng.sample(hosts, len(hosts))
        n = rng.randint(*step.hosts)
        hosts = picks[step.host_role][step.host_skip: step.host_skip + n]
        base = rng.uniform(*step.at)
        reps = rng.randint(*step.repeat)
        for host in hosts:
            h_off = base + (rng.uniform(0, step.spread) if step.spread else 0.0)
            for r in range(reps):
                off = h_off + r * step.every + (rng.uniform(0, step.every * 0.2) if r and step.every else 0.0)
                value = None
                if step.value:
                    value = round(rng.uniform(*step.value) + r * step.value_step, 1)
                res = off + rng.uniform(*step.resolve) if step.resolve else None
                plan.append((off, res, step, host, value, r))
    return plan


def expand_scenario(sc: Scenario, customer: Customer, root_ts: float, rng: random.Random, incident_id: str, *,
                    orphan: bool = False, compress_to: Optional[float] = None) -> list[dict]:
    """Turn one scenario occurrence into events. `orphan` keeps only the precursors and labels
    them as noise (they lead to nothing). `compress_to` squeezes the timeline into that many
    simulated minutes for the live stream."""
    plan = plan_scenario(sc, customer, rng, roles={"precursor"} if orphan else None)
    if not plan:
        return []
    f = (lambda x: x)
    if compress_to:
        f = compress_fn([p[0] for p in plan] + [p[1] for p in plan if p[1] is not None], compress_to)
    out: list[dict] = []
    for off, res, step, host, value, _r in plan:
        if orphan:
            truth = _truth(f"orphan:{sc.id}")
            if res is None and step.source in ("netapp", "checkmk", "zabbix", "alertmanager", "dynatrace"):
                res = off + rng.uniform(45, 240)   # the warning clears on its own
        else:
            truth = _truth(f"scenario:{sc.id}", step.role, sc.id, incident_id)
        ts = root_ts + f(off) * 60
        rts = root_ts + f(res) * 60 if res is not None else None
        _fire_and_resolve(out, customer, step.source, step.check, host, step.host_role, ts, rng, truth,
                          value=value, threshold=step.threshold, native=step.native, extra=step.extra or None,
                          resolve_ts=rts)
    return out


def scenario_lead_min(sc: Scenario, span_min: float) -> float:
    """How far before the root the earliest compressed precursor sits, worst case (minutes)."""
    lo = min([s.at[0] for s in sc.steps] + [0.0])
    return min(-lo, 0.4 * span_min) if lo < 0 else 0.0


# ---------------------------------------------------------------- history

def _finalise(events: list[dict], id_prefix: str) -> list[dict]:
    events.sort(key=lambda e: (e["ts"], e["source"], e["host"], e["check"], e["status"], e["title"], e["_nid"]))
    for i, e in enumerate(events, 1):
        e.pop("_nid", None)
        e.pop("_picks", None)
        e["id"] = f"{id_prefix}{i:06d}"
    return [{"id": e.pop("id"), **e} for e in events]


def _pick_root_times(sc: Scenario, n: int, rng: random.Random, start: float, end: float,
                     windows: dict[str, list[tuple[float, float]]], taken: list[tuple[str, float]]) -> list[tuple[str, float]]:
    elig = sc.eligible_customers()
    lead_days = math.ceil(-min([s.at[0] for s in sc.steps] + [0.0]) / 1440) + 1
    out: list[tuple[str, float]] = []
    if sc.patch_night:
        pool = [(cid, w) for cid in elig for w in windows.get(cid, [])]
        rng.shuffle(pool)
        for cid, w in pool[:n]:
            out.append((cid, w[0] + rng.uniform(40, 150) * 60))
        return out
    tries = 0
    while len(out) < n and tries < 500:
        tries += 1
        cid = rng.choice(elig)
        day = rng.randint(lead_days, 29)
        base = start + day * 86400
        if sc.hours:
            hour = rng.uniform(*sc.hours) - LOCAL_OFFSET_H
        else:
            hour = rng.uniform(0, 24)
        ts = base + hour * 3600
        if not (start + 3600 < ts < end - 6 * 3600):
            continue
        if any(c == cid and abs(t - ts) < 2 * 86400 for c, t in taken + out):
            continue
        out.append((cid, ts))
    return out


def generate_history(seed: Any, end_iso: str, days: int = 30) -> tuple[list[dict], dict]:
    """Return (events sorted by time with ids, summary). Deterministic for a given seed."""
    end = parse_iso(end_iso)
    start = end - days * 86400
    events: list[dict] = []
    windows: dict[str, list[tuple[float, float]]] = {}
    for cid, cust in CUSTOMERS.items():
        events.extend(noise_events(cust, start, end, 3600, seed))
        windows[cid] = patch_windows(cust, start, end)
        for w in windows[cid]:
            events.extend(patch_night_events(cust, w, seed))
    summary: dict[str, Any] = {"scenarios": {}, "orphans": {}, "patch_nights": sum(len(w) for w in windows.values())}
    for sc in SCENARIOS.values():
        taken: list[tuple[str, float]] = []      # same scenario, same customer: at least 2 days apart
        rng = _rng(seed, "scenario", sc.id)
        n = rng.randint(*sc.occurrences)
        roots = _pick_root_times(sc, n, rng, start, end, windows, taken)
        taken.extend(roots)
        for k, (cid, ts) in enumerate(roots, 1):
            inc = f"{sc.id}#{k}"
            events.extend(expand_scenario(sc, CUSTOMERS[cid], ts, _rng(seed, inc), inc))
        summary["scenarios"][sc.id] = len(roots)
        if sc.orphans[1]:
            no = rng.randint(*sc.orphans)
            oroots = _pick_root_times(sc, no, rng, start, end, windows, taken)
            for k, (cid, ts) in enumerate(oroots, 1):
                events.extend(expand_scenario(sc, CUSTOMERS[cid], ts, _rng(seed, sc.id, "orphan", k), "",
                                              orphan=True))
            summary["orphans"][sc.id] = len(oroots)
    events = [e for e in events if start <= parse_iso(e["ts"]) < end]
    out = _finalise(events, "h-")
    summary["events"] = len(out)
    summary["start"] = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary["end"] = end_iso
    return out, summary
