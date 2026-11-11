"""Spend ledger and caps for every LLM call.

Every request to a model goes through `Budget.reserve()` before it is sent and
`Budget.record()` after it returns (or fails). `reserve()` enforces the caps from
config.yaml and raises `BudgetRefused` when a call is not allowed; nothing is sent then.

Caps (config.yaml `caps`), all over a sliding hour of real time:
- calls_per_hour_global, usd_per_hour_global: every call of every role
- calls_per_hour_session, usd_per_hour_session: calls that name a `session` (a visitor, an
  engineer run). Calls without a session (the shared live triage) only count globally.
- the hard `max_tokens` per role (config.yaml `models.<role>.max_tokens`): `max_tokens()`
  clamps any request to it.

Cost = prompt tokens x input price + completion tokens x output price, prices in USD per 1M
tokens from config.yaml `prices`. The ledger keeps one hour of records; `cost_last(60)` and
`totals()` feed the UI's cost readout (`snapshot()`).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable, Optional

HOUR = 3600.0


class BudgetRefused(Exception):
    """A cap would be exceeded; the call was not sent. `reason` is a short machine word,
    `retry_in_s` an estimate of when the oldest counted call leaves the window."""

    def __init__(self, reason: str, retry_in_s: float = 0.0, detail: str = ""):
        super().__init__(f"budget refused: {reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.retry_in_s = retry_in_s
        self.detail = detail


@dataclass
class CallRecord:
    id: int
    ts: float
    role: str
    model: str
    session: Optional[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = False
    done: bool = False
    estimated: bool = False          # usage not reported by the API, counted from text length


@dataclass
class Reservation:
    """Handle returned by `reserve()`; pass it to `record()` once the call finished."""
    record: CallRecord
    max_tokens: int


@dataclass
class _Totals:
    calls: int = 0
    failed: int = 0
    refused: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    by_role: dict[str, dict[str, float]] = field(default_factory=dict)


class Budget:
    def __init__(self, cfg: dict[str, Any], time_fn: Callable[[], float] = time.time):
        self.cfg = cfg
        self.caps = cfg.get("caps", {})
        self.prices = cfg.get("prices", {})
        self.models = cfg.get("models", {})
        self.time_fn = time_fn
        self._ids = count(1)
        self._ledger: deque[CallRecord] = deque()
        self._totals = _Totals()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    # ------------------------------------------------------------ config lookups
    def model_for(self, role: str) -> str:
        return self.models[role]["id"]

    def max_tokens(self, role: str, requested: Optional[int] = None) -> int:
        """The hard per-role cap; a request can ask for less, never more."""
        cap = int(self.models[role].get("max_tokens", 1000))
        return cap if requested is None else max(1, min(int(requested), cap))

    def price(self, model: str) -> tuple[float, float]:
        p = self.prices.get(model) or {}
        return float(p.get("input", 0.0)), float(p.get("output", 0.0))

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pin, pout = self.price(model)
        return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000

    # ------------------------------------------------------------ ledger
    def _trim(self, now: float) -> None:
        while self._ledger and now - self._ledger[0].ts > HOUR:
            self._ledger.popleft()

    def _window(self, now: float, session: Optional[str] = None) -> tuple[int, float, Optional[float]]:
        calls, usd, oldest = 0, 0.0, None
        for r in self._ledger:
            if session is not None and r.session != session:
                continue
            calls += 1
            usd += r.cost_usd
            oldest = r.ts if oldest is None else oldest
        return calls, usd, oldest

    def check(self, role: str, session: Optional[str] = None) -> None:
        """Raise BudgetRefused if a call of this role (and session) would break a cap. Sends
        nothing and records nothing: probes use it to test the budget for free."""
        if role not in self.models:
            raise BudgetRefused("unknown_role", 0.0, role)
        now = self.time_fn()
        self._trim(now)
        calls, usd, oldest = self._window(now)
        retry = max(0.0, HOUR - (now - oldest)) if oldest is not None else 0.0
        if calls >= int(self.caps.get("calls_per_hour_global", 10**9)):
            raise BudgetRefused("calls_per_hour_global", retry)
        if usd >= float(self.caps.get("usd_per_hour_global", float("inf"))):
            raise BudgetRefused("usd_per_hour_global", retry)
        if session is not None:
            s_calls, s_usd, s_oldest = self._window(now, session)
            s_retry = max(0.0, HOUR - (now - s_oldest)) if s_oldest is not None else 0.0
            if s_calls >= int(self.caps.get("calls_per_hour_session", 10**9)):
                raise BudgetRefused("calls_per_hour_session", s_retry)
            if s_usd >= float(self.caps.get("usd_per_hour_session", float("inf"))):
                raise BudgetRefused("usd_per_hour_session", s_retry)

    def reserve(self, role: str, session: Optional[str] = None, max_tokens: Optional[int] = None) -> Reservation:
        """Count a call before it is sent. Raises BudgetRefused (and counts the refusal)."""
        try:
            self.check(role, session)
        except BudgetRefused:
            self._totals.refused += 1
            self._notify()
            raise
        rec = CallRecord(id=next(self._ids), ts=self.time_fn(), role=role, model=self.model_for(role),
                         session=session)
        self._ledger.append(rec)
        return Reservation(record=rec, max_tokens=self.max_tokens(role, max_tokens))

    def record(self, res: Reservation, prompt_tokens: int = 0, completion_tokens: int = 0, *,
               ok: bool = True, estimated: bool = False) -> CallRecord:
        """Book the usage of a finished (or failed) call."""
        rec = res.record
        if rec.done:
            return rec
        rec.prompt_tokens = int(prompt_tokens or 0)
        rec.completion_tokens = int(completion_tokens or 0)
        rec.cost_usd = self.cost(rec.model, rec.prompt_tokens, rec.completion_tokens)
        rec.ok, rec.done, rec.estimated = ok, True, estimated
        t = self._totals
        t.calls += 1
        t.failed += 0 if ok else 1
        t.prompt_tokens += rec.prompt_tokens
        t.completion_tokens += rec.completion_tokens
        t.cost_usd += rec.cost_usd
        r = t.by_role.setdefault(rec.role, {"calls": 0, "cost_usd": 0.0})
        r["calls"] += 1
        r["cost_usd"] += rec.cost_usd
        self._notify()
        return rec

    # ------------------------------------------------------------ readouts
    def cost_last(self, seconds: float = 60.0) -> float:
        now = self.time_fn()
        return sum(r.cost_usd for r in self._ledger if now - r.ts <= seconds)

    def calls_last(self, seconds: float = 60.0) -> int:
        now = self.time_fn()
        return sum(1 for r in self._ledger if now - r.ts <= seconds)

    def totals(self) -> dict[str, Any]:
        t = self._totals
        return {"calls": t.calls, "failed": t.failed, "refused": t.refused,
                "prompt_tokens": t.prompt_tokens, "completion_tokens": t.completion_tokens,
                "cost_usd": round(t.cost_usd, 6),
                "by_role": {k: {"calls": int(v["calls"]), "cost_usd": round(v["cost_usd"], 6)}
                            for k, v in t.by_role.items()}}

    def snapshot(self) -> dict[str, Any]:
        """What the browser gets (`budget` SSE event): no keys, no prompts, just money."""
        now = self.time_fn()
        self._trim(now)
        calls_h, usd_h, _ = self._window(now)
        return {"usd_last_min": round(self.cost_last(60), 6), "calls_last_min": self.calls_last(60),
                "usd_last_hour": round(usd_h, 6), "calls_last_hour": calls_h,
                "usd_cap_hour": float(self.caps.get("usd_per_hour_global", 0) or 0),
                "totals": self.totals()}

    def _notify(self) -> None:
        if not self.listeners:
            return
        snap = self.snapshot()
        for fn in list(self.listeners):
            try:
                fn(snap)
            except Exception:
                pass

    def reset_totals(self) -> None:
        """Totals shown in the UI restart (the hourly ledger, which enforces caps, is kept)."""
        self._totals = _Totals()
