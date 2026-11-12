"""Live triage: fold the alerts that got past the rules into a few incident cards.

Every `caps.triage_interval_s` real seconds (8 s), while at least one browser is connected:

1. Take the new survivors (`LiveSim.listeners`): firing events of normalised severity minor and
   above, at most `caps.triage_max_lines` (60) compact lines, most severe first; the rest is
   carried to the next tick (bounded, and dropped after `line_max_age_min`).
2. Ask the triage model (one JSON call) to attach them to open incidents, open new ones, or
   call them noise. Unknown incident ids and event ids are dropped; an event lands on at most
   one card; a new incident that duplicates a card on the board is merged into it.
3. Stream a 2-3 sentence plain-language summary for each new or materially changed card (at
   most `max_summaries_per_tick`), token by token as `incident_delta`; `incident_upsert`
   carries the structured card.
4. Age the board in SIMULATED time: no new evidence for `quiet_after_min` (10) and at least half
   of its alerts resolved, or no new evidence for `quiet_unresolved_after_min` (30) at all ->
   `quiet` (dimmed, not counted as "needs a human"; green "recovering" once 80 % have resolved,
   so an outage that is still down does not fade while its alerts keep firing). A card whose
   alerts have ALL resolved goes quiet after only `recovered_quiet_min` (3) without new evidence,
   and a new incident whose alerts have all resolved already (a blip that cleared within the
   tick, typically a branch router back after a few minutes) opens no card at all. Quiet for
   `expire_after_min` (45, i.e. ~45 real s)
   -> the card leaves the board. The board holds at most `max_cards` (6) cards; beyond that only
   major/critical incidents may open, retiring the oldest quiet (then oldest minor) card.
   "Got it" (dismiss) hides a card for good; it keeps collecting related alerts silently for
   `ack_keep_min` so the model does not reopen it.

Degraded mode: after `failures_to_degrade` (2) consecutive failed triage calls, or at once on a
budget refusal, the loop groups alerts deterministically by customer and title keywords
(`degraded_groups`) with templated summaries (`origin: degraded`) and tells the UI to show
"AI paused, rule-based grouping". Every `probe_every_s` (30) it tries the model again with the
real batch; one success resumes AI triage.

Quiet stream (nothing minor or above): no model call. No browser connected: the tick does
nothing at all (the simulated clock is paused too), so an idle desk spends nothing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.llm.budget import Budget, BudgetRefused
from app.llm.client import LLMClient, LLMError
from app.models import Event, Incident
from app.prompts.triage import event_line, summary_messages, triage_messages
from app.sim.catalog import CUSTOMERS, iso
from app.sim.generator import parse_iso

log = logging.getLogger("signal_desk.triage")

SEV_RANK = {"info": 0, "warning": 1, "minor": 2, "major": 3, "critical": 4}
TRIAGE_SEVERITIES = {"minor", "major", "critical"}
INCIDENT_SEVERITIES = ("critical", "major", "minor")
EVIDENCE_PREVIEW = 5
EVIDENCE_KEEP = 400                    # evidence events kept per card (the count keeps going)
RESUMMARY_MIN_REAL_S = 30.0
SUMMARY_MAX_TOKENS = 220


def triage_worthy(e: Event) -> bool:
    """A firing alert the triage model should see: minor and above, plus every early warning an
    accepted precursor rule raised (whatever severity the rule gave it)."""
    if e.status != "firing":
        return False
    return e.severity in TRIAGE_SEVERITIES or (bool(e.produced_by) and e.check == "early_warning")

# deterministic keyword families for degraded mode: (key, label, team, keywords). First match
# wins; matching is case-insensitive on the title.
FAMILIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("certificate", "Certificate and portal availability", "Web Operations",
     ("certificate", "tls", "probe of https")),
    ("backup", "Backup and storage", "Backup & Storage",
     ("volume space", "backup job", "/backup", "snapshot reserve", "repository")),
    ("database", "Database", "Database Team",
     ("sql server", "postgresql", "blocking process", "deadlock", "query", "page life")),
    ("network", "Network reachability", "Network Operations",
     ("interface", "link monitor", "ping", "unreach", "is down", "icmp", "heartbeat",
      "failed to connect", "vpn", "node ")),
    ("jobs", "Batch jobs", "Batch Operations", ("job ",)),
    ("kubernetes", "Kubernetes workloads", "Platform Team",
     ("pod ", "oomkilled", "container", "hpa ", "throttling")),
    ("web", "Application errors", "Web Operations",
     ("failure rate", "response time", "http 5xx", "application gateway", "synthetic monitor")),
    ("security", "Security signals", "Security Operations",
     ("sign-in", "login", "powershell", "unwanted", "pup", "suspicious")),
    ("capacity", "Capacity and thresholds", "the platform owner",
     ("cpu", "memory", "disk", "filesystem", "swap", "agent", "unusual")),
)
OTHER_FAMILY = ("other", "Other alerts", "the service owner", ())


def family_of(title: str) -> tuple[str, str, str]:
    t = title.lower()
    for key, label, team, words in FAMILIES:
        if any(w in t for w in words):
            return key, label, team
    return OTHER_FAMILY[0], OTHER_FAMILY[1], OTHER_FAMILY[2]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.6


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


_STOP = frozenset("and all the has its for with from this that are was not but into onto its their any "
                  "now also been have which".split())


def _content(s: str) -> set[str]:
    return _words(s) - _STOP


def repeats(sentence: str, cause: str) -> bool:
    """True if a summary sentence restates the card's likely cause (most of the cause's content
    words are in it, e.g. "core switch X lost Te1/1/1, cutting off the access switches ...")."""
    ws, wc = _content(sentence), _content(cause)
    if not ws or not wc:
        return False
    inter = len(ws & wc)
    return inter / len(ws | wc) >= 0.5 or inter / len(wc) >= 0.6


def drop_repeated_cause(summary: str, cause: str) -> str:
    """The card prints the likely cause right under the summary: take out summary sentences that
    say the same thing again. If every sentence repeats it, keep the one that repeats it least."""
    sents = _sentences(summary)
    if not sents or not cause:
        return summary
    keep = [s for s in sents if not repeats(s, cause)]
    if keep:
        return " ".join(keep)
    wc = _content(cause)
    return min(sents, key=lambda s: len(_content(s) & wc) / max(1, len(_content(s) | wc)))


def fallback_summary(c: "Card", customer_short: str) -> str:
    """Deterministic card text when the summary call fails (never a copy of the likely cause)."""
    hosts = len({e.host for e in c.events})
    tools = len({e.source for e in c.events})
    since = min((e.ts for e in c.events), default="")
    who = f" at {customer_short}" if customer_short else ""
    return (f"Alerts from {tools} tool{'s' if tools != 1 else ''} on {hosts} host{'s' if hosts != 1 else ''}"
            f"{who} since {_local_hhmm(since) if since else 'just now'}. The written summary is not "
            f"available right now; the evidence lines below are complete.")


def _clip(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


@dataclass
class Card:
    inc: Incident
    last_evidence: float                      # simulated ts of the newest evidence
    quiet_since: Optional[float] = None
    family: Optional[str] = None              # degraded grouping key
    events: list[Event] = field(default_factory=list)
    evidence_total: int = 0
    keys: set[tuple[str, str, str]] = field(default_factory=set)       # (source, host, check)
    resolved: set[tuple[str, str, str]] = field(default_factory=set)
    summarised_count: int = 0
    summarised_rank: int = -1
    summarised_at: float = -1e9               # real time
    summary_state: str = "pending"            # pending | streaming | done
    removed: bool = False

    @property
    def id(self) -> str:
        return self.inc.id

    @property
    def visible(self) -> bool:
        return not self.removed and self.inc.status in ("open", "quiet")


class TriageLoop:
    def __init__(self, sim: Any, hub: Any, llm: Optional[LLMClient], budget: Budget, cfg: dict[str, Any],
                 time_fn: Callable[[], float] = time.monotonic):
        self.sim = sim
        self.hub = hub
        self.llm = llm
        self.budget = budget
        caps = cfg.get("caps", {})
        t = cfg.get("triage", {})
        self.interval_s = float(caps.get("triage_interval_s", 8))
        self.max_lines = int(caps.get("triage_max_lines", 60))
        self.quiet_after = float(t.get("quiet_after_min", 10)) * 60
        self.quiet_unresolved_after = float(t.get("quiet_unresolved_after_min", 30)) * 60
        self.recovered_after = float(t.get("recovered_quiet_min", 3)) * 60
        self.expire_after = float(t.get("expire_after_min", 45)) * 60
        self.ack_keep = float(t.get("ack_keep_min", 120)) * 60
        self.max_cards = int(t.get("max_cards", 6))
        self.max_new = int(t.get("max_new_per_tick", 3))
        self.max_summaries = int(t.get("max_summaries_per_tick", 2))
        self.failures_to_degrade = int(t.get("failures_to_degrade", 2))
        self.probe_every = float(t.get("probe_every_s", 30))
        self.carry_max = int(t.get("carry_max", 120))
        self.line_max_age = float(t.get("line_max_age_min", 15)) * 60
        self.time_fn = time_fn
        self._inbox: deque[Event] = deque(maxlen=5000)
        sim.listeners.append(self.on_survivors)
        self.reset_state()

    # ------------------------------------------------------------ state
    def reset_state(self) -> None:
        self.cards: dict[str, Card] = {}
        self._inbox.clear()
        self._carry: list[Event] = []
        self._resolved_at: dict[tuple[str, str, str], float] = {}     # latest resolve per (source, host, check)
        self._n = 0
        self.failures = 0
        self.degraded = False
        self.degraded_reason = ""
        self._last_probe = -1e9
        self.stats = Counter()
        self._busy = False

    def reset(self) -> None:
        self.reset_state()
        self.hub.publish("triage_state", self.state_payload())

    def on_survivors(self, events: list[Event]) -> None:
        """LiveSim listener: everything that got past the rules this step."""
        self._inbox.extend(events)

    # ------------------------------------------------------------ payloads (no truth, ever)
    def card_payload(self, c: Card) -> dict[str, Any]:
        evs = sorted(c.events, key=lambda e: (-SEV_RANK.get(e.severity, 0), e.ts))
        seen, preview = set(), []
        for e in evs:                                   # varied preview: one line per tool+title
            k = (e.source, e.title)
            if k in seen:
                continue
            seen.add(k)
            preview.append({"id": e.id, "ts": e.ts, "vendor": e.vendor, "source": e.source, "host": e.host,
                            "severity": e.severity, "native_severity": e.native_severity, "title": e.title,
                            "by_rule": bool(e.produced_by)})
            if len(preview) >= EVIDENCE_PREVIEW:
                break
        cust = CUSTOMERS.get(c.inc.customer or "")
        recovering = bool(c.keys) and c.inc.status == "quiet" and len(c.resolved & c.keys) >= 0.8 * len(c.keys)
        return {
            **c.inc.model_dump(exclude={"evidence"}),
            "customer_name": cust.name if cust else (c.inc.customer or ""),
            "customer_short": cust.short if cust else (c.inc.customer or ""),
            "evidence_count": c.evidence_total,
            "tool_count": len({e.source for e in c.events}),
            "evidence": preview,
            "more": max(0, c.evidence_total - len(preview)),
            "summary_state": c.summary_state,
            "recovering": recovering,
        }

    def board(self) -> list[dict[str, Any]]:
        cards = [c for c in self.cards.values() if c.visible]
        cards.sort(key=lambda c: (c.inc.status != "open", -SEV_RANK[c.inc.severity], c.inc.opened_ts or ""),)
        return [self.card_payload(c) for c in cards]

    def open_count(self) -> int:
        return sum(1 for c in self.cards.values() if c.visible and c.inc.status == "open")

    def state_payload(self) -> dict[str, Any]:
        return {"degraded": self.degraded, "reason": self.degraded_reason, "open": self.open_count(),
                "ai_available": self.llm is not None}

    def snapshot(self) -> dict[str, Any]:
        return {"incidents": self.board(), "triage": self.state_payload(), "budget": self.budget.snapshot()}

    def _upsert(self, c: Card) -> None:
        self.hub.publish("incident_upsert", {"incident": self.card_payload(c), "open": self.open_count()})

    def _remove(self, c: Card, reason: str) -> None:
        log.info("removed %s (%s): %s", c.id, reason, c.inc.title)
        self.hub.publish("incident_remove", {"id": c.id, "reason": reason, "open": self.open_count()})

    # ------------------------------------------------------------ dismiss
    def dismiss(self, inc_id: str) -> bool:
        c = self.cards.get(inc_id)
        if c is None or c.removed:
            return False
        if c.inc.status != "acknowledged":
            c.inc.status = "acknowledged"
            self._remove(c, "dismissed")
            self.hub.publish("triage_state", self.state_payload())
        return True

    # ------------------------------------------------------------ evidence
    def _new_id(self) -> str:
        self._n += 1
        return f"INC-{self.sim.epoch}{self._n:03d}"

    def _add_evidence(self, c: Card, events: list[Event]) -> None:
        for e in events:
            k = (e.source, e.host, e.check)
            c.events.append(e)
            c.keys.add(k)
            # its resolve may already have come in (blips clear within the same tick)
            if self._resolved_at.get(k, 0.0) >= parse_iso(e.ts):
                c.resolved.add(k)
            else:
                c.resolved.discard(k)
            c.last_evidence = max(c.last_evidence, parse_iso(e.ts))
        c.evidence_total += len(events)
        if len(c.events) > EVIDENCE_KEEP:
            c.events = c.events[:20] + c.events[-(EVIDENCE_KEEP - 20):]   # keep the first (root) lines
        c.inc.evidence = [e.id for e in c.events][-60:]
        c.inc.updated_ts = iso(c.last_evidence)
        if c.inc.status == "quiet":
            c.inc.status = "open"
            c.quiet_since = None

    def _note_resolves(self, events: list[Event]) -> None:
        resolved = {(e.source, e.host, e.check) for e in events if e.status == "resolved"}
        if not resolved:
            return
        for e in events:
            if e.status == "resolved":
                k = (e.source, e.host, e.check)
                self._resolved_at[k] = max(self._resolved_at.get(k, 0.0), parse_iso(e.ts))
        if len(self._resolved_at) > 20_000:          # bounded: forget the oldest half
            for k, _ in sorted(self._resolved_at.items(), key=lambda kv: kv[1])[:10_000]:
                del self._resolved_at[k]
        for c in self.cards.values():
            if not c.removed and c.keys & resolved:
                c.resolved |= (c.keys & resolved)

    def _age(self, now: float) -> None:
        changed_state = False
        for c in list(self.cards.values()):
            if c.removed:
                continue
            idle = now - c.last_evidence
            if c.inc.status == "acknowledged":
                if idle > self.ack_keep:
                    c.removed = True
                continue
            cleared = bool(c.keys) and len(c.resolved & c.keys) >= 0.5 * len(c.keys)
            recovered = bool(c.keys) and c.keys <= c.resolved          # every alert on it has cleared
            if c.inc.status == "open" and (idle >= self.quiet_unresolved_after
                                           or (idle >= self.quiet_after and cleared)
                                           or (idle >= self.recovered_after and recovered)):
                c.inc.status = "quiet"
                c.quiet_since = now
                self._upsert(c)
                changed_state = True
            elif c.inc.status == "quiet":
                if c.quiet_since is not None and now - c.quiet_since >= self.expire_after:
                    c.removed = True
                    self._remove(c, "expired")
                    changed_state = True
                elif c.keys and len(c.resolved & c.keys) >= 0.8 * len(c.keys):
                    self._upsert(c)          # recovering flag may have changed
        for cid in [k for k, c in self.cards.items() if c.removed]:
            del self.cards[cid]
        if changed_state:
            self.hub.publish("triage_state", self.state_payload())

    def _make_room(self, severity: str) -> bool:
        """Board full: retire the oldest quiet card, then the oldest minor one, for a major or
        critical incident. Minor incidents do not displace anything."""
        visible = [c for c in self.cards.values() if c.visible]
        if len(visible) < self.max_cards:
            return True
        if SEV_RANK.get(severity, 0) < SEV_RANK["major"]:
            return False
        for pool in ([c for c in visible if c.inc.status == "quiet"],
                     [c for c in visible if c.inc.severity == "minor"]):
            if pool:
                victim = min(pool, key=lambda c: c.last_evidence)
                victim.removed = True
                self._remove(victim, "board_full")
                del self.cards[victim.id]
                return True
        return True        # all major/critical: let it through rather than hide a real outage

    # ------------------------------------------------------------ batch selection
    def _select(self, now: float) -> tuple[list[Event], int]:
        fresh = list(self._inbox)
        self._inbox.clear()
        self._note_resolves(fresh)
        cand = self._carry + [e for e in fresh if triage_worthy(e)]
        cand = [e for e in cand if now - parse_iso(e.ts) <= self.line_max_age]
        seen: set[str] = set()
        uniq = []
        for e in cand:
            if e.id not in seen:
                seen.add(e.id)
                uniq.append(e)
        ranked = sorted(uniq, key=lambda e: (-SEV_RANK[e.severity], e.ts))
        batch = ranked[: self.max_lines]
        rest = ranked[self.max_lines:]
        # carry the most severe of the rest, newest first, bounded
        rest.sort(key=lambda e: (-SEV_RANK[e.severity], e.ts), reverse=False)
        self._carry = rest[: self.carry_max]
        batch.sort(key=lambda e: e.ts)
        return batch, len(rest)

    # ------------------------------------------------------------ the tick
    async def tick(self) -> dict[str, Any]:
        if self.hub.client_count == 0:
            return {"skipped": "idle"}
        if self._busy:
            return {"skipped": "busy"}
        self._busy = True
        try:
            return await self._tick()
        finally:
            self._busy = False

    async def _tick(self) -> dict[str, Any]:
        now = self.sim.clock.now()
        batch, omitted = self._select(now)
        self._age(now)
        out: dict[str, Any] = {"lines": len(batch), "omitted": omitted, "mode": "degraded" if self.degraded else "ai"}
        if not batch:
            self.hub.publish("budget", self.budget.snapshot())
            out["skipped"] = "quiet"
            return out
        use_ai = self.llm is not None and (not self.degraded or self.time_fn() - self._last_probe >= self.probe_every)
        if use_ai:
            if self.degraded:
                self._last_probe = self.time_fn()
                out["probe"] = True
            ok = await self._ai_triage(batch, omitted, now, out)
            if ok:
                out["mode"] = "ai"
                await self._summaries()
            elif self.degraded:
                self._degraded_triage(batch, now)
                out["mode"] = "degraded"
            else:
                self._carry = (batch + self._carry)[: self.carry_max]     # retry these next tick
        else:
            if self.llm is None and not self.degraded:
                self._set_degraded(True, "no_model")
            self._degraded_triage(batch, now)
        self.hub.publish("budget", self.budget.snapshot())
        return out

    def _set_degraded(self, on: bool, reason: str = "") -> None:
        if on == self.degraded and reason == self.degraded_reason:
            return
        self.degraded = on
        self.degraded_reason = reason if on else ""
        if on:
            self._last_probe = self.time_fn()
            log.warning("triage degraded: %s", reason)
        else:
            log.info("triage resumed with AI")
        self.hub.publish("triage_state", self.state_payload())

    async def _ai_triage(self, batch: list[Event], omitted: int, now: float, out: dict[str, Any]) -> bool:
        board = []
        for c in self.cards.values():
            if c.removed:
                continue
            p = self.card_payload(c)
            # the card's first lines (its root) and its newest; no ids: only new lines are referable
            picks = c.events[:2] + [e for e in c.events[-2:] if e not in c.events[:2]]
            latest = [event_line(e, "-") for e in picks]
            board.append({**p, "latest": latest})
        # short per-tick aliases (e1, e2, ...) instead of event ids: fewer tokens in and out
        alias = {f"e{n}": e.id for n, e in enumerate(batch, 1)}
        lines = [event_line(e, f"e{n}") for n, e in enumerate(batch, 1)]
        msgs = triage_messages(lines, board, CUSTOMERS, iso(now)[11:19], omitted)
        try:
            result, usage = await self.llm.json_call("triage", msgs)
        except BudgetRefused as e:
            self.stats["budget_refused"] += 1
            out["error"] = f"budget:{e.reason}"
            self._set_degraded(True, "budget")
            return False
        except LLMError as e:
            self.failures += 1
            self.stats["llm_errors"] += 1
            out["error"] = e.kind
            log.warning("triage call failed (%s), %d in a row", e, self.failures)
            if self.failures >= self.failures_to_degrade:
                self._set_degraded(True, e.kind)
            return False
        self.failures = 0
        if self.degraded:
            self._set_degraded(False)
        out["usage"] = {"in": usage.prompt_tokens, "out": usage.completion_tokens, "usd": round(usage.cost_usd, 6)}
        out.update(self.apply_result(dealias(result, alias), batch))
        return True

    # ------------------------------------------------------------ merge rules
    def apply_result(self, result: Any, batch: list[Event]) -> dict[str, int]:
        """Apply the model's {attach, new, noise_ids}. Unknown ids are dropped; each event lands
        on at most one card. Returns counts for logs and tests."""
        sent = {e.id: e for e in batch}
        used: set[str] = set()
        counts = Counter()
        if not isinstance(result, dict):
            return dict(counts)

        def take(ids: Any, customer: Optional[str] = None) -> list[Event]:
            got = []
            for i in (ids if isinstance(ids, list) else []):
                i = str(i).strip()
                e = sent.get(i)
                if e is None:
                    counts["unknown_event_ids"] += 1
                    continue
                if i in used or (customer and e.customer != customer):
                    continue
                used.add(i)
                got.append(e)
            return got

        touched: dict[str, Card] = {}
        for a in attach_entries(result.get("attach")):
            c = self.cards.get(str(a.get("incident_id", "")).strip())
            if c is None or c.removed:
                counts["unknown_incidents"] += 1
                continue
            evs = take(a.get("event_ids"), c.inc.customer)
            # sanity guard: a threshold-type alert (CPU, disk, agent, unusual traffic ...) only
            # joins a card that already has evidence on the same host
            hosts = {e.host for e in c.events}
            keep = [e for e in evs if family_of(e.title)[0] != "capacity" or e.host in hosts]
            if len(keep) < len(evs):
                counts["attach_rejected"] += len(evs) - len(keep)
                for e in evs:
                    if e not in keep:
                        used.discard(e.id)
            evs = keep
            if evs:
                self._add_evidence(c, evs)
                touched[c.id] = c
                counts["attached"] += len(evs)
                # severity can only rise, and only with new evidence
                sev = a.get("severity")
                if sev in INCIDENT_SEVERITIES and SEV_RANK[sev] > SEV_RANK[c.inc.severity]:
                    log.info("raised %s %s -> %s", c.id, c.inc.severity, sev)
                    c.inc.severity = sev
                    counts["raised"] += 1

        news = result.get("new") if isinstance(result.get("new"), list) else []
        opened = 0
        for n in news:
            if not isinstance(n, dict):
                continue
            ids = [str(i).strip() for i in (n.get("event_ids") if isinstance(n.get("event_ids"), list) else [])]
            counts["unknown_event_ids"] += sum(1 for i in ids if i not in sent)
            cands = [sent[i] for i in dict.fromkeys(ids) if i in sent and i not in used]
            if not cands:
                counts["new_dropped"] += 1
                continue
            cust = str(n.get("customer", "")).strip()
            if cust not in CUSTOMERS:
                cust = Counter(e.customer for e in cands).most_common(1)[0][0]
            evs = take([e.id for e in cands], cust)
            if not evs:
                counts["new_dropped"] += 1
                continue
            # a blip that has already cleared (every alert in it resolved, e.g. a branch router
            # that was back within minutes) needs nobody: no card
            if all(self._resolved_at.get((e.source, e.host, e.check), 0.0) >= parse_iso(e.ts) for e in evs):
                counts["new_recovered"] += 1
                log.info("not opened (already recovered): %s", _clip(n.get("title") or evs[0].title, 90))
                continue
            sev = str(n.get("severity", "")).strip().lower()
            if sev not in INCIDENT_SEVERITIES:
                sev = max((e.severity for e in evs), key=lambda s: SEV_RANK[s])
                sev = sev if sev in INCIDENT_SEVERITIES else "minor"
            title = _clip(n.get("title") or evs[0].title, 90)
            # the same incident already on the board (or acknowledged): merge, do not duplicate
            dup = next((c for c in self.cards.values() if not c.removed and c.inc.customer == cust
                        and _similar(c.inc.title, title)), None)
            if dup is not None:
                self._add_evidence(dup, evs)
                touched[dup.id] = dup
                counts["merged"] += 1
                log.info("merged new '%s' into %s (%d alerts)", title, dup.id, len(evs))
                continue
            if opened >= self.max_new or not self._make_room(sev):
                for e in evs:
                    used.discard(e.id)
                counts["new_capped"] += 1
                continue
            first = min(parse_iso(e.ts) for e in evs)
            inc = Incident(id=self._new_id(), title=title, severity=sev, origin="llm", customer=cust,
                           likely_cause=_clip(n.get("likely_cause"), 240), next_step=_clip(n.get("next_step"), 260),
                           opened_ts=iso(first), updated_ts=iso(first), status="open")
            c = Card(inc=inc, last_evidence=first)
            self._add_evidence(c, evs)
            self.cards[c.id] = c
            touched[c.id] = c
            opened += 1
            counts["opened"] += 1
            log.info("opened %s [%s] %s: %s (%d alerts)", c.id, sev, cust, title, len(evs))
        counts["noise"] = len([i for i in (result.get("noise_ids") or []) if str(i) in sent]) \
            if isinstance(result.get("noise_ids"), list) else 0
        for c in touched.values():
            if c.visible:
                self._upsert(c)
        if touched:
            self.hub.publish("triage_state", self.state_payload())
        return dict(counts)

    # ------------------------------------------------------------ summaries
    def _needs_summary(self, c: Card) -> bool:
        if not c.visible or c.inc.origin != "llm" or c.summary_state == "streaming":
            return False
        if c.summary_state == "pending":
            return True
        rank = SEV_RANK[c.inc.severity]
        grew = c.evidence_total >= max(c.summarised_count + 3, int(1.5 * c.summarised_count))
        recent = self.time_fn() - c.summarised_at < RESUMMARY_MIN_REAL_S
        return (rank > c.summarised_rank or grew) and not recent

    async def _summaries(self) -> None:
        todo = [c for c in self.cards.values() if self._needs_summary(c)]
        todo.sort(key=lambda c: (c.summary_state != "pending", -SEV_RANK[c.inc.severity], c.inc.opened_ts or ""))
        todo = todo[: self.max_summaries]
        if todo:
            await asyncio.gather(*(self._stream_summary(c) for c in todo))

    async def _stream_summary(self, c: Card) -> None:
        payload = self.card_payload(c)
        evs = sorted(c.events, key=lambda e: (-SEV_RANK.get(e.severity, 0), e.ts))[:12]
        evs.sort(key=lambda e: e.ts)
        cust = CUSTOMERS.get(c.inc.customer or "")
        previous = c.inc.summary if c.summary_state == "done" else None
        msgs = summary_messages(payload, [event_line(e) for e in evs], cust.short if cust else "", previous)
        c.summary_state = "streaming"
        count_at, rank_at = c.evidence_total, SEV_RANK[c.inc.severity]
        self.hub.publish("incident_delta", {"id": c.id, "start": True, "delta": ""})
        text = ""
        try:
            stream = self.llm.stream_text("render", msgs, max_tokens=SUMMARY_MAX_TOKENS)
            async for piece in stream:
                text += piece
                self.hub.publish("incident_delta", {"id": c.id, "delta": piece})
            c.inc.summary = (_clip(drop_repeated_cause(stream.text, c.inc.likely_cause), 600)
                             or c.inc.summary or fallback_summary(c, cust.short if cust else ""))
        except (LLMError, BudgetRefused) as e:
            log.warning("summary for %s failed: %s", c.id, getattr(e, "kind", getattr(e, "reason", e)))
            c.inc.summary = c.inc.summary or fallback_summary(c, cust.short if cust else "")
        c.summary_state = "done"
        c.summarised_count, c.summarised_rank, c.summarised_at = count_at, rank_at, self.time_fn()
        if c.visible:
            self._upsert(c)

    # ------------------------------------------------------------ degraded mode
    def degraded_groups(self, batch: list[Event]) -> list[tuple[tuple[str, str], list[Event]]]:
        """Deterministic grouping: (customer, keyword family) -> events, in a stable order."""
        groups: dict[tuple[str, str], list[Event]] = {}
        for e in sorted(batch, key=lambda e: (e.ts, e.id)):
            groups.setdefault((e.customer, family_of(e.title)[0]), []).append(e)
        return sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1]))

    def _degraded_text(self, c: Card) -> None:
        fam = next((f for f in FAMILIES if f[0] == c.family), OTHER_FAMILY)
        hosts = Counter(e.host for e in c.events)
        titles = Counter(re.sub(r"^Resolved: ", "", e.title) for e in c.events)
        top_host = hosts.most_common(1)[0][0]
        top_title = titles.most_common(1)[0][0]
        tools = len({e.source for e in c.events})
        since = min(e.ts for e in c.events)
        n = c.evidence_total
        c.inc.summary = (f"{n} alert{'s' if n != 1 else ''} from {tools} tool{'s' if tools != 1 else ''} since "
                         f"{_local_hhmm(since)}, on {len(hosts)} host{'s' if len(hosts) != 1 else ''}, most on "
                         f"{top_host}. Grouped by keywords while the AI is paused; not analysed.")
        c.inc.likely_cause = f"Not analysed. Most frequent alert: “{_clip(top_title, 90)}”."
        c.inc.next_step = f"Start with {top_host}. Call {fam[2]} if this is not a known issue."
        sev = max((e.severity for e in c.events), key=lambda s: SEV_RANK[s])
        c.inc.severity = sev if sev in INCIDENT_SEVERITIES else "minor"

    def _degraded_triage(self, batch: list[Event], now: float) -> None:
        opened = 0
        for (cust, fam), evs in self.degraded_groups(batch):
            hosts = {e.host for e in evs}
            target = next((c for c in self.cards.values() if not c.removed and c.inc.customer == cust
                           and c.family == fam), None)
            if target is None:   # an AI card for the same customer and hosts takes them too
                target = next((c for c in self.cards.values() if not c.removed and c.inc.customer == cust
                               and c.inc.origin == "llm" and hosts & {e.host for e in c.events}), None)
            if target is not None:
                self._add_evidence(target, evs)
                if target.inc.origin == "degraded":
                    self._degraded_text(target)
                if target.visible:
                    self._upsert(target)
                continue
            serious = any(SEV_RANK[e.severity] >= SEV_RANK["major"] for e in evs)
            # threshold blips, security chatter and unclassified alerts need a major/critical
            # alert to open a card; the operational families open on three alerts as well
            routine = fam in ("capacity", "security", "other")
            if not (serious or (len(evs) >= 3 and not routine)) or opened >= self.max_new:
                continue
            if all(self._resolved_at.get((e.source, e.host, e.check), 0.0) >= parse_iso(e.ts) for e in evs):
                continue                                   # already recovered: nobody needs to act

            sev = max((e.severity for e in evs), key=lambda s: SEV_RANK[s])
            if not self._make_room(sev):
                continue
            label = next((f[1] for f in FAMILIES if f[0] == fam), OTHER_FAMILY[1])
            cobj = CUSTOMERS.get(cust)
            first = min(parse_iso(e.ts) for e in evs)
            inc = Incident(id=self._new_id(), title=f"{label} alerts at {cobj.short if cobj else cust}",
                           severity=sev if sev in INCIDENT_SEVERITIES else "minor", origin="degraded",
                           customer=cust, opened_ts=iso(first), updated_ts=iso(first), status="open")
            c = Card(inc=inc, last_evidence=first, family=fam, summary_state="done")
            self._add_evidence(c, evs)
            self._degraded_text(c)
            self.cards[c.id] = c
            opened += 1
            self._upsert(c)
        self.hub.publish("triage_state", self.state_payload())

    # ------------------------------------------------------------ asyncio loop
    async def run(self) -> None:
        while True:
            started = self.time_fn()
            try:
                if self.hub.client_count == 0:
                    await asyncio.sleep(0.5)
                    continue
                res = await self.tick()
                if res.get("lines") or res.get("error"):
                    log.info("triage tick %s", {k: v for k, v in res.items() if k != "usage"} | (
                        {"usd": res["usage"]["usd"]} if "usage" in res else {}))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("triage loop error")
            await asyncio.sleep(max(0.5, self.interval_s - (self.time_fn() - started)))


def attach_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalise the model's `attach` into [{incident_id, event_ids}]. Accepts the asked-for
    objects and the shapes small models drift into: [[id, [ids]], ...] and {id: [ids]}."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        if "incident_id" in raw:
            raw = [raw]
        else:
            raw = [[k, v] for k, v in raw.items()]
    if not isinstance(raw, list):
        return out
    for a in raw:
        if isinstance(a, dict) and "incident_id" in a:
            ids = a.get("event_ids")
            entry = {"incident_id": str(a["incident_id"]), "event_ids": ids if isinstance(ids, list) else []}
            if isinstance(a.get("severity"), str):
                entry["severity"] = a["severity"].strip().lower()
            out.append(entry)
        elif isinstance(a, (list, tuple)) and len(a) == 2 and isinstance(a[1], list):
            out.append({"incident_id": str(a[0]), "event_ids": a[1]})
    return out


def dealias(result: Any, alias: dict[str, str]) -> Any:
    """Map the per-tick aliases in a model answer back to event ids. Unknown refs pass through
    unchanged (and are then dropped by `apply_result` as unknown ids)."""
    if not isinstance(result, dict):
        return result

    def m(ids: Any) -> Any:
        return [alias.get(str(i).strip(), str(i)) for i in ids] if isinstance(ids, list) else ids

    out = dict(result)
    out["attach"] = [{**a, "event_ids": m(a.get("event_ids"))} for a in attach_entries(result.get("attach"))]
    if isinstance(result.get("new"), list):
        out["new"] = [{**n, "event_ids": m(n.get("event_ids"))} if isinstance(n, dict) else n for n in result["new"]]
    out["noise_ids"] = m(result.get("noise_ids"))
    return out


def _local_hhmm(ts: str) -> str:
    """HH:MM in CET (UTC+1), like the UI clock."""
    t = parse_iso(ts) + 3600
    return iso(t)[11:16]
