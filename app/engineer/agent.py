"""The rule engineer: a strong model in a visible tool loop over the 30-day history, then a
server-side check of everything it proposes.

Flow of one live run (`EngineerService.start_run`):
1. The model works with the tools in app/engineer/tools.py (limits from config.yaml `caps`:
   `engineer_max_tool_calls` tool calls in total, `engineer_max_seconds` of wall time; it is
   forced to submit when one call is left or `FORCE_SUBMIT_MARGIN_S` before the deadline). Every
   tool call becomes one human-readable `engineer_log` line; a short clean sentence the model
   writes between calls becomes a "thinking out loud" line. Empty model output is retried once.
2. `submit_proposals` ends the loop. The server then revalidates every rule and re-runs the
   backtest itself (the model's own numbers are never used), and drops proposals that
   - are invalid, or duplicate an active rule or an earlier proposal,
   - newly hide an incident,
   - remove under `MIN_SAVED_PCT` % of the alerts (all kinds except early warnings),
   - are early warnings with no warning that was right, or fewer than `MIN_PRECURSOR_PRECISION`
     of their warnings right (a false-alarm ratio above 2 to 1).
3. For each survivor one cheap `render` call drafts the rule in the operator's own tool syntax
   (`native_draft`, always "Illustrative draft"); a failed draft never loses the proposal.
4. Proposals stream as `engineer_proposal`, then `engineer_done` carries a one-line total computed
   by backtesting the whole set together. A run with at least 2 survivors is saved to
   data/saved_run.json; `load_saved()` replays it (log quickly, then cards) against the CURRENT
   active rules, offline.

Guards: one run at a time; at most one run start per `engineer_cooldown_s` (global); a run with
no browser connected for `ABANDON_AFTER_S` is cancelled; the budget caps every model call (session
"engineer").
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import DATA_DIR
from app.engine.backtest import backtest, backtest_set, load_history
from app.engine.rules import describe, rule_to_dict, validate
from app.engineer.tools import SUBMIT, TOOL_SCHEMAS, ToolContext, dumps, run_tool, sample_alert
from app.llm.budget import BudgetRefused
from app.llm.client import LLMError
from app.models import BacktestResult, NativeDraft, Proposal
from app.prompts.engineer import draft_messages, engineer_messages

log = logging.getLogger("signal_desk.engineer")

SESSION = "engineer"
MAX_PROPOSALS = 5
MIN_SAVED_PCT = 1.0                 # non-precursor proposals must remove at least 1 % of the alerts
MIN_PRECURSOR_PRECISION = 1 / 3     # at least one early warning in three must be right
MIN_LEAD_MIN = 10.0                 # ... and the right ones must come at least 10 min (median) ahead
FORCE_SUBMIT_MARGIN_S = 40.0        # force the submit this long before the wall-time limit
ABANDON_AFTER_S = 20.0              # no browser connected this long: cancel the run
REPLAY_LINE_S = 0.12                # saved run: delay between work-log lines
MIN_SAVE_SURVIVORS = 2
SAVED_RUN_PATH = DATA_DIR / "saved_run.json"
KIND_ORDER = {"suppress": 0, "retune": 0, "correlate": 0, "precursor": 1}
FORCE_SUBMIT_TEXT = ("Time is up: no more analysis. Call submit_proposals now with your best 3 to 5 backtested "
                     "rules (title, kind, reasoning, rule).")


class EngineerRefused(Exception):
    def __init__(self, reason: str, retry_in_s: float = 0.0, message: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.retry_in_s = retry_in_s
        self.message = message


class RunFailed(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rule_identity(rule: Any) -> str:
    """What a rule is about, ignoring its tuning: two rules with the same identity are duplicates."""
    k = rule.kind
    if k == "retune":
        ident: Any = [rule.source, rule.check]
    elif k == "correlate":
        ident = sorted(json.dumps(m.model_dump(exclude_none=True), sort_keys=True) for m in rule.members)
    else:
        ident = rule.match.model_dump(exclude_none=True)
    return json.dumps([k, ident], sort_keys=True)


def clean_thought(text: str) -> Optional[str]:
    """A short assistant sentence that is fit to show as "thinking out loud", or None."""
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text or "", flags=re.DOTALL).strip()
    if not t or any(ch in t for ch in "{}[]`|#*"):
        return None
    t = re.sub(r"\s+", " ", t)
    first = re.split(r"(?<=[.!?])\s", t)[0].strip()
    if len(first) < 12 or len(first) > 220:
        return None
    return first


def drop_reason(rule: Any, bt: BacktestResult) -> Optional[str]:
    """Why the server drops a proposal after its own backtest (None = keep)."""
    if bt.incidents_hidden > 0:
        return f"hides {bt.incidents_hidden} incident{'s' if bt.incidents_hidden != 1 else ''}"
    if rule.kind == "precursor":
        if bt.precursor_true < 1:
            return "no early warning was followed by an incident"
        if bt.precursor_true / max(1, bt.precursor_fired) < MIN_PRECURSOR_PRECISION:
            return f"too many false alarms ({bt.precursor_false} of {bt.precursor_fired})"
        if (bt.median_lead_min or 0) < MIN_LEAD_MIN:
            return "warns too late to help (it fires with the failure itself)"
        return None
    if bt.saved_pct < MIN_SAVED_PCT:
        return f"saves only {bt.saved_pct:g}% of the alerts"
    return None


class EngineerService:
    def __init__(self, llm: Any, hub: Any, cfg: dict[str, Any], *, active_fn: Callable[[], list],
                 activate_fn: Callable[..., Any], budget: Any = None, history: Any = None,
                 saved_path: Optional[Path] = None, time_fn: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        caps = cfg.get("caps", {})
        self.llm = llm
        self.hub = hub
        self.budget = budget
        self.active_fn = active_fn
        self.activate_fn = activate_fn
        self.history = history
        self.saved_path = Path(saved_path) if saved_path else SAVED_RUN_PATH
        self.time_fn = time_fn
        self.sleep = sleep
        self.max_calls = int(caps.get("engineer_max_tool_calls", 14))
        self.max_s = float(caps.get("engineer_max_seconds", 180))
        self.cooldown_s = float(caps.get("engineer_cooldown_s", 600))
        self.model = cfg.get("models", {}).get("engineer", {}).get("id", "")
        self.task: Optional[asyncio.Task] = None
        self.last_start: Optional[float] = None
        self._idle = 0.0
        self._clear()

    # ------------------------------------------------------------ state
    def _clear(self) -> None:
        self.state = "idle"               # idle | running | replaying | done | failed
        self.run_id: Optional[str] = None
        self.mode: Optional[str] = None   # live | saved
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.log: list[dict[str, Any]] = []
        self.proposals: list[Proposal] = []
        self.summary = ""
        self.error: Optional[dict[str, Any]] = None
        self.saved_date: Optional[str] = None
        self.stats: dict[str, Any] = {}
        self.spent_usd = 0.0              # what this live run has cost so far (model + drafts)

    def saved_info(self) -> Optional[dict[str, Any]]:
        try:
            d = json.loads(self.saved_path.read_text())
            if isinstance(d.get("proposals"), list) and d["proposals"]:
                return {"date": d.get("date"), "saved_at": d.get("saved_at"), "count": len(d["proposals"])}
        except Exception:
            pass
        return None

    def cooldown_left(self) -> float:
        if self.last_start is None:
            return 0.0
        return max(0.0, self.cooldown_s - (self.time_fn() - self.last_start))

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def state_payload(self) -> dict[str, Any]:
        saved = self.saved_info()
        elapsed = None
        if self.started is not None:
            elapsed = round((self.finished or self.time_fn()) - self.started, 1)
        return {"state": self.state, "run_id": self.run_id, "mode": self.mode, "elapsed_s": elapsed,
                "max_s": self.max_s, "log": list(self.log), "summary": self.summary, "error": self.error,
                "cooldown_s": round(self.cooldown_left(), 1), "saved_available": saved is not None,
                "saved_date": (saved or {}).get("date"), "replay_date": self.saved_date,
                # one-off cost of this run, shown next to it (a replayed saved run costs nothing)
                "cost_usd": round(self.spent_usd, 4) if self.mode == "live" else None,
                "pending": sum(1 for p in self.proposals if p.status == "proposed")}

    def proposal_payload(self, p: Proposal) -> dict[str, Any]:
        d = p.model_dump(by_alias=True, exclude_none=True)
        d["rule"] = rule_to_dict(p.rule)
        d["rule_json"] = json.dumps(d["rule"], indent=2, ensure_ascii=False)
        return d

    def snapshot(self) -> dict[str, Any]:
        return {"engineer": self.state_payload(), "proposals": [self.proposal_payload(p) for p in self.proposals]}

    def _publish(self, kind: str, data: dict[str, Any]) -> None:
        try:
            self.hub.publish(kind, data)
        except Exception:                      # pragma: no cover - a publish must never kill a run
            log.exception("publish %s failed", kind)

    def _publish_state(self) -> None:
        self._publish("engineer_state", {"engineer": self.state_payload()})

    def _log(self, text: str, kind: str = "tool") -> None:
        t = round(self.time_fn() - (self.started or self.time_fn()), 1)
        entry = {"t": t, "text": text, "kind": kind}
        self.log.append(entry)
        self._publish("engineer_log", {"run_id": self.run_id, "entry": entry})

    def _fail(self, reason: str, message: str = "") -> None:
        self.state = "failed"
        self.finished = self.time_fn()
        saved = self.saved_info()
        self.error = {"reason": reason, "message": message, "saved_available": saved is not None,
                      "saved_date": (saved or {}).get("date")}
        log.warning("engineer run %s failed: %s %s", self.run_id, reason, message)
        self._publish("engineer_error", {"run_id": self.run_id, **self.error})
        self._publish_state()

    def _active(self) -> list:
        return list(self.active_fn())

    # ------------------------------------------------------------ public actions
    def start_run(self) -> str:
        if self.busy():
            raise EngineerRefused("busy", 0.0, "a run is already in progress")
        left = self.cooldown_left()
        if left > 0:
            raise EngineerRefused("cooldown", left)
        if self.budget is not None:
            try:
                self.budget.check("engineer", SESSION)
            except BudgetRefused as e:
                raise EngineerRefused("budget", e.retry_in_s, e.reason) from None
        self._clear()
        self.run_id = uuid.uuid4().hex[:8]
        self.mode = "live"
        self.state = "running"
        self.started = self.last_start = self.time_fn()
        self._publish_state()
        self.task = asyncio.create_task(self._run_live(self.run_id), name=f"engineer-{self.run_id}")
        return self.run_id

    def load_saved(self) -> str:
        if self.busy():
            raise EngineerRefused("busy", 0.0, "a run is already in progress")
        data = self._read_saved()
        if data is None:
            raise EngineerRefused("no_saved_run")
        self._clear()
        self.run_id = uuid.uuid4().hex[:8]
        self.mode = "saved"
        self.state = "replaying"
        self.saved_date = data.get("date")
        self.started = self.time_fn()
        self._publish_state()
        self.task = asyncio.create_task(self._run_saved(self.run_id, data), name=f"engineer-saved-{self.run_id}")
        return self.run_id

    def get(self, pid: str) -> Optional[Proposal]:
        return next((p for p in self.proposals if p.id == pid), None)

    def accept(self, pid: str) -> tuple[Proposal, Any]:
        p = self.get(pid)
        if p is None:
            raise KeyError(pid)
        if p.status != "proposed":
            raise ValueError(f"proposal is already {p.status}")
        ar = self.activate_fn(p.rule, p.title, proposal_id=p.id)
        p.status = "accepted"
        p.active_rule_id = getattr(ar, "id", None)
        self._publish("engineer_proposal", {"run_id": self.run_id, "proposal": self.proposal_payload(p)})
        self._publish_state()
        return p, ar

    def reject(self, pid: str) -> Proposal:
        p = self.get(pid)
        if p is None:
            raise KeyError(pid)
        if p.status != "proposed":
            raise ValueError(f"proposal is already {p.status}")
        p.status = "rejected"
        self._publish("engineer_proposal", {"run_id": self.run_id, "proposal": self.proposal_payload(p)})
        self._publish_state()
        return p

    async def refresh_pending(self) -> None:
        """Re-run the backtests of the pending proposals against the current active rules (after an
        accept), so their numbers stay true."""
        active = self._active()
        for p in [p for p in self.proposals if p.status == "proposed"]:
            try:
                p.backtest = await asyncio.to_thread(backtest, p.rule, active, self.history)
            except Exception:                  # pragma: no cover
                continue
            if p.status == "proposed":
                self._publish("engineer_proposal", {"run_id": self.run_id, "proposal": self.proposal_payload(p)})

    def reset(self) -> None:
        if self.busy():
            self.task.cancel()
        self.task = None
        self._clear()
        self._publish_state()

    # ------------------------------------------------------------ live run
    async def _run_live(self, run_id: str) -> None:
        work = asyncio.create_task(self._live(run_id))
        try:
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=1.0)
                if done:
                    break
                if self.hub.client_count == 0:
                    self._idle += 1.0
                    if self._idle >= ABANDON_AFTER_S:
                        work.cancel()
                        try:
                            await work
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._fail("cancelled", "nobody was watching")
                        return
                else:
                    self._idle = 0.0
            await work
        except asyncio.CancelledError:
            work.cancel()
            raise
        except RunFailed as e:
            self._fail(e.reason, e.message)
        except Exception as e:                  # noqa: BLE001
            log.exception("engineer run crashed")
            self._fail("error", type(e).__name__)
        finally:
            self._idle = 0.0

    async def _live(self, run_id: str) -> None:
        active = self._active()
        ctx = ToolContext(active=active, hist=self.history)
        try:
            submitted, calls, cost = await asyncio.wait_for(self._agent_loop(ctx, active), timeout=self.max_s)
        except asyncio.TimeoutError:
            if self.stats.get("model_calls_ok", 0) == 0:
                self.last_start = None
            raise RunFailed("timeout", f"no proposals within {self.max_s:.0f} s") from None
        self.stats.update(tool_calls=calls - self.stats.get("refused_calls", 0))
        survivors = await self._finalise(submitted, active, origin="live")
        render_cost = await self._draft_and_publish(survivors, run_id)
        self.stats["cost_usd"] = round(cost + render_cost, 4)
        self._done(survivors, active)
        if len(survivors) >= MIN_SAVE_SURVIVORS and self.state == "done":
            self._save(survivors)

    async def _call_model(self, messages: list[dict], tool_choice: Any, deadline: float) -> Any:
        """One engineer turn; empty output is retried once. Raises RunFailed."""
        for attempt in (1, 2):
            timeout = max(5.0, min(float(self.llm.role_spec("engineer").get("timeout_s", 60)),
                                   deadline - self.time_fn()))
            try:
                turn = await self.llm.tool_call("engineer", messages, TOOL_SCHEMAS, tool_choice=tool_choice,
                                                session=SESSION, timeout=timeout)
            except BudgetRefused as e:
                if self.stats.get("model_calls_ok", 0) == 0:
                    self.last_start = None           # nothing was spent: no cooldown
                raise RunFailed("budget", e.reason) from None
            except LLMError as e:
                if e.kind in ("empty", "server", "timeout", "connection") and attempt == 1:
                    log.warning("engineer turn failed (%s), retrying once", e.kind)
                    continue
                if self.stats.get("model_calls_ok", 0) == 0:
                    self.last_start = None
                raise RunFailed("ai_unavailable" if e.kind != "empty" else "empty", e.kind) from None
            self.stats["model_calls_ok"] = self.stats.get("model_calls_ok", 0) + 1
            return turn
        raise RunFailed("empty", "no output")       # pragma: no cover

    async def _agent_loop(self, ctx: ToolContext, active: list) -> tuple[list[dict], int, float]:
        active_view = [{"title": a.title, "description": describe(a.rule)} for a in active]
        messages = engineer_messages(self.max_calls, active_view)
        start = self.time_fn()
        deadline = start + self.max_s
        calls = 0
        cost = 0.0
        nudges = 0
        submit_retry_used = False
        forced_turns = 0
        self._log("Started: reading 30 days of alert history", "info")
        while True:
            left = self.max_calls - calls
            force = left <= 1 or self.time_fn() - start > self.max_s - FORCE_SUBMIT_MARGIN_S
            if force:
                # A forced tool_choice is refused by the engineer model on OpenRouter (400), so the
                # submit is asked for in words and every other tool is refused from here on.
                forced_turns += 1
                if forced_turns > 3:
                    raise RunFailed("no_proposals", "the model did not hand in its proposals")
                if forced_turns == 1:
                    messages.append({"role": "user", "content": FORCE_SUBMIT_TEXT})
            turn = await self._call_model(messages, "auto", deadline)
            turn_cost = float(getattr(turn.usage, "cost_usd", 0.0) or 0.0)
            cost += turn_cost
            self.spent_usd += turn_cost
            messages.append(turn.message)
            thought = clean_thought(turn.content) if turn.content else None
            if thought:
                self._log(thought, "think")
            if not turn.tool_calls:
                nudges += 1
                if nudges > 2:
                    raise RunFailed("no_proposals", "the model stopped without submitting")
                messages.append({"role": "user", "content": "Continue with the tools. Finish with one "
                                                            "submit_proposals call."})
                continue
            submitted: Optional[list[dict]] = None
            for tc in turn.tool_calls:
                calls += 1
                if tc.parse_error:
                    out: dict[str, Any] = {"ok": False, "error": "arguments were not valid JSON; send a JSON object"}
                    self._log(f"Sent unreadable arguments to {tc.name}; asked to resend")
                elif tc.name == SUBMIT:
                    props = tc.arguments.get("proposals") if isinstance(tc.arguments, dict) else None
                    if not isinstance(props, list):
                        props = []
                    props = [p for p in props if isinstance(p, dict)][:MAX_PROPOSALS]
                    errors = []
                    for i, p in enumerate(props):
                        _, errs = validate(p.get("rule"))
                        if errs:
                            errors.append(f"proposal {i + 1} ({str(p.get('title', ''))[:40]}): {errs[0]}")
                    can_retry = (not submit_retry_used and calls < self.max_calls
                                 and self.time_fn() - start < self.max_s - 20)
                    if (errors or not props) and can_retry:
                        submit_retry_used = True
                        out = {"ok": False, "errors": errors or ["no proposals received"],
                               "hint": "fix these rules (backtest them) and call submit_proposals again with the full list"}
                        self._log(f"Submitted {len(props)} proposals; the rule engine refused "
                                  f"{len(errors) if errors else 'them'}, sent back to fix")
                    else:
                        submitted = props
                        out = {"ok": True, "received": len(props)}
                        self._log(f"Handed in {len(props)} proposal{'s' if len(props) != 1 else ''} "
                                  f"after {calls - self.stats.get('refused_calls', 0)} tool calls", "info")
                elif calls >= self.max_calls or force:
                    # the last of the max_calls is reserved for submit_proposals
                    out = {"ok": False, "error": "tool budget used up: call submit_proposals now"}
                    self.stats["refused_calls"] = self.stats.get("refused_calls", 0) + 1
                else:
                    out, line = await asyncio.to_thread(run_tool, tc.name, tc.arguments, ctx)
                    self._log(line)
                    log.info("run %s tool %s %s", self.run_id, tc.name, tc.raw_arguments[:400])
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": dumps(out)})
            if submitted is not None:
                return submitted, calls, cost

    # ------------------------------------------------------------ server-side check
    async def _finalise(self, cands: list[dict], active: list, origin: str) -> list[Proposal]:
        """Revalidate and re-backtest each candidate; the model's numbers are never used."""
        seen = {rule_identity(a.rule) for a in active}
        keep: list[Proposal] = []
        dropped: list[str] = []
        for c in cands[:MAX_PROPOSALS]:
            title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:80] or "Proposed rule"
            rule, errs = validate(c.get("rule"))
            if errs:
                dropped.append(f"{title}: invalid")
                continue
            ident = rule_identity(rule)
            if ident in seen:
                dropped.append(f"{title}: already active or proposed")
                continue
            bt = await asyncio.to_thread(backtest, rule, active, self.history)
            why = drop_reason(rule, bt)
            if why:
                dropped.append(f"{title}: {why}")
                continue
            seen.add(ident)
            reasoning = re.sub(r"\s+", " ", str(c.get("reasoning") or "")).strip()[:600]
            draft = None
            nd = c.get("native_draft")
            if origin == "saved" and isinstance(nd, dict):
                try:
                    draft = NativeDraft.model_validate(nd)
                except Exception:
                    draft = None
            keep.append(Proposal(id=f"p-{uuid.uuid4().hex[:8]}", kind=rule.kind, title=title, reasoning=reasoning,
                                 rule=rule, description=describe(rule), backtest=bt, native_draft=draft,
                                 origin=origin))
        keep.sort(key=lambda p: (KIND_ORDER[p.kind], -p.backtest.alerts_saved if p.kind != "precursor" else 0))
        n = len(cands[:MAX_PROPOSALS])
        if dropped:
            self._log(f"Rule engine re-checked {n} proposals: kept {len(keep)}, dropped {len(dropped)} "
                      f"({'; '.join(dropped)})", "info")
        else:
            self._log(f"Rule engine re-checked {n} proposals on 30 days of history: all {len(keep)} hold up", "info")
        self.stats["dropped"] = dropped
        return keep

    async def _draft(self, p: Proposal) -> tuple[Optional[NativeDraft], float]:
        try:
            sample = await asyncio.to_thread(sample_alert, p.rule, self.history)
        except Exception:                       # noqa: BLE001 - the draft works without it
            sample = None
        dialect, msgs = draft_messages(p.rule, p.title, sample)
        try:
            data, usage = await self.llm.json_call("render", msgs, session=SESSION, max_tokens=700)
            text = str(data.get("text") or "").strip()
            if not text:
                return None, float(usage.cost_usd or 0.0)
            text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()[:2500]
            return NativeDraft(dialect=dialect, text=text), float(usage.cost_usd or 0.0)
        except (LLMError, BudgetRefused) as e:
            log.warning("native draft for %s failed: %s", p.id, getattr(e, "kind", getattr(e, "reason", e)))
        except Exception:                       # noqa: BLE001 - a draft must never lose a proposal
            log.exception("native draft for %s crashed", p.id)
        return None, 0.0

    async def _draft_and_publish(self, survivors: list[Proposal], run_id: str) -> float:
        if survivors:
            self._log("Writing each rule in the syntax of the tool it is about (illustrative drafts)", "info")
        tasks = [asyncio.create_task(self._draft(p)) for p in survivors]
        cost = 0.0
        for p, t in zip(survivors, tasks):
            draft, c = await t
            cost += c
            self.spent_usd += c
            p.native_draft = draft
            self.proposals.append(p)
            self._publish("engineer_proposal", {"run_id": run_id, "proposal": self.proposal_payload(p)})
        return cost

    def _done(self, survivors: list[Proposal], active: list) -> None:
        self.finished = self.time_fn()
        if not survivors:
            self._fail("no_proposals", "none of the proposals held up in the backtest")
            return
        try:
            combo = backtest_set([p.rule for p in survivors], active, history=self.history)
        except Exception:                       # pragma: no cover
            combo = None
        n = len(survivors)
        head = f"{n} rule{'s' if n != 1 else ''} proposed"
        if combo is None:
            self.summary = head + "."
        else:
            hidden = ("hidden none of the" if combo.incidents_hidden == 0
                      else f"hidden {combo.incidents_hidden} of the")
            if combo.alerts_saved > 0:
                together = "together they would have" if n > 1 else "it would have"
                self.summary = (f"{head}; {together} removed {combo.saved_pct:g}% of last month's alerts and "
                                f"{hidden} {combo.incidents_total} incidents.")
            else:
                self.summary = f"{head}; {'they' if n > 1 else 'it'} would have {hidden} {combo.incidents_total} incidents."
            self.stats["combined"] = combo.model_dump()
        self.state = "done"
        self._publish("engineer_done", {"run_id": self.run_id, "summary": self.summary, "count": n,
                                        "mode": self.mode, "replay_date": self.saved_date})
        self._publish_state()
        log.info("engineer run %s done: %s", self.run_id, self.summary)

    # ------------------------------------------------------------ saved run
    def _save(self, survivors: list[Proposal]) -> None:
        now = _now_iso()
        data = {
            "version": 1, "saved_at": now, "date": now[:10], "model": self.model,
            "duration_s": round((self.finished or self.time_fn()) - (self.started or 0.0), 1),
            "tool_calls": self.stats.get("tool_calls"), "cost_usd": self.stats.get("cost_usd"),
            "summary": self.summary, "log": self.log,
            "proposals": [{"title": p.title, "kind": p.kind, "reasoning": p.reasoning, "rule": rule_to_dict(p.rule),
                           "native_draft": p.native_draft.model_dump() if p.native_draft else None}
                          for p in survivors],
        }
        try:
            self.saved_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.saved_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self.saved_path)
            log.info("saved engineer run to %s", self.saved_path.name)
        except OSError:
            log.exception("could not save the engineer run")

    def _read_saved(self) -> Optional[dict[str, Any]]:
        try:
            d = json.loads(self.saved_path.read_text())
        except Exception:
            return None
        if not isinstance(d, dict) or not isinstance(d.get("proposals"), list) or not d["proposals"]:
            return None
        return d

    async def _run_saved(self, run_id: str, data: dict[str, Any]) -> None:
        try:
            for entry in data.get("log") or []:
                if not isinstance(entry, dict) or not entry.get("text"):
                    continue
                e = {"t": entry.get("t", 0), "text": str(entry["text"])[:400], "kind": entry.get("kind", "tool")}
                self.log.append(e)
                self._publish("engineer_log", {"run_id": run_id, "entry": e})
                await self.sleep(REPLAY_LINE_S)
            active = self._active()
            self._log(f"Replaying the saved run from {data.get('date')}: re-checking its rules against the rules "
                      f"active now", "info")
            survivors = await self._finalise(data["proposals"], active, origin="saved")
            for p in survivors:
                self.proposals.append(p)
                self._publish("engineer_proposal", {"run_id": run_id, "proposal": self.proposal_payload(p)})
                await self.sleep(REPLAY_LINE_S * 2)
            self._done(survivors, active)
        except asyncio.CancelledError:
            raise
        except Exception as e:                  # noqa: BLE001
            log.exception("saved run replay crashed")
            self._fail("error", type(e).__name__)
