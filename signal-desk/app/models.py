"""Shared Pydantic schemas: events, incidents, the rule DSL, backtests, proposals.

These are the contracts between the simulator, the rule engine, the triage loop, the rule
engineer and the browser. Structural checks live here (unknown keys, ranges, list sizes);
semantic checks that need the catalog (unknown sources, match-all rules, suppress touching
critical alerts) belong to app/engine/rules.py.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- events

Severity = Literal["critical", "major", "minor", "warning", "info"]
SEVERITIES: tuple[str, ...] = ("critical", "major", "minor", "warning", "info")
EventStatus = Literal["firing", "resolved"]
TruthRole = Literal["root", "symptom", "precursor", "noise"]


class Truth(BaseModel):
    """Hidden ground truth. Only the backtest may read it: never the model, the agent tools
    or the browser."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: Optional[str] = None
    role: TruthRole = "noise"
    # one id per scenario occurrence, shared by all its events; None for noise
    incident_id: Optional[str] = None
    # which designed pattern produced the event, e.g. "noise:checkmk_ntp",
    # "tight:zabbix_cpu", "group:branch_vpn_blip", "orphan:backup_disk_fills", "patch_night"
    pattern: Optional[str] = None


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ts: str                       # ISO 8601 UTC, simulated time, e.g. 2026-11-10T09:31:12Z
    customer: str                 # customer id, e.g. "nordhafen"
    source: str                   # source id, e.g. "dynatrace"
    vendor: str                   # display name, e.g. "Dynatrace"
    host: str
    service: str
    check: str                    # stable check name within the source (retune key)
    title: str
    severity: Severity            # normalised
    native_severity: str          # the vendor's own word, e.g. "Sev2", "Disaster", "CRIT"
    status: EventStatus
    value: Optional[float] = None
    threshold: Optional[float] = None
    raw: dict[str, Any] = Field(default_factory=dict)
    # set on events a rule made: an early warning (precursor) or a grouped alert (correlate)
    produced_by: Optional[str] = None          # active rule id
    children: list[str] = Field(default_factory=list)   # grouped / triggering event ids
    truth: Optional[Truth] = None

    def public(self) -> dict[str, Any]:
        """The event as the browser, the models and the agent tools may see it."""
        return self.model_dump(exclude={"truth"})


# ---------------------------------------------------------------- incidents

class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    summary: str = ""
    likely_cause: str = ""
    next_step: str = ""
    severity: Severity
    evidence: list[str] = Field(default_factory=list)      # event ids
    origin: Literal["llm", "degraded"] = "llm"
    customer: Optional[str] = None
    opened_ts: Optional[str] = None
    updated_ts: Optional[str] = None
    status: Literal["open", "quiet", "acknowledged"] = "open"   # quiet: no new evidence for a while


# ---------------------------------------------------------------- rule DSL

class Match(BaseModel):
    """Shared selector. All given fields must match (AND). An empty match is structurally
    valid here; the engine validator rejects match-all rules."""

    model_config = ConfigDict(extra="forbid")

    source: Optional[str] = None
    customer: Optional[str] = None
    host_glob: Optional[str] = None
    title_contains: Optional[str] = None
    severity_in: Optional[list[Severity]] = None


WindowMin = Annotated[int, Field(ge=1, le=1440)]


class RaiseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=120)
    severity: Severity


class SuppressRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["suppress"] = "suppress"
    match: Match
    window_min: WindowMin
    max_per_window: int = Field(ge=0, le=1000)


class PrecursorRule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["precursor"] = "precursor"
    match: Match
    count: int = Field(ge=1, le=1000)
    within_min: WindowMin
    raise_: RaiseSpec = Field(alias="raise")
    # how far ahead the warning is meant to come: the backtest credits it only if the incident it
    # points to starts within this many hours (default 6 when not given; at most 72)
    horizon_h: Optional[int] = Field(None, ge=1, le=72)


class RetuneRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["retune"] = "retune"
    source: str
    check: str
    op: Literal[">", ">=", "<", "<="]
    new_threshold: float
    sustain_min: Annotated[int, Field(ge=0, le=1440)] = 0


class CorrelateRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["correlate"] = "correlate"
    members: list[Match] = Field(min_length=2, max_length=4)
    within_min: WindowMin
    keep: int = Field(0, ge=0, le=3)   # index of the member whose alert stays visible


Rule = Annotated[
    Union[SuppressRule, PrecursorRule, RetuneRule, CorrelateRule],
    Field(discriminator="kind"),
]
RuleKind = Literal["suppress", "precursor", "retune", "correlate"]


class ActiveRule(BaseModel):
    """A rule that is live (or switched off) in the engine."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    rule: Rule
    enabled: bool = True
    live_since_ts: Optional[str] = None     # simulated time it went live (strip marker)
    proposal_id: Optional[str] = None


# ---------------------------------------------------------------- backtest and proposals

class BacktestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts_before: int
    alerts_after: int
    alerts_saved: int
    saved_pct: float
    incidents_total: int
    incidents_visible: int
    incidents_hidden: int
    precursor_fired: int = 0
    precursor_true: int = 0
    precursor_false: int = 0
    median_lead_min: Optional[float] = None
    samples: list[str] = Field(default_factory=list, max_length=5)  # suppressed lines


NativeDialect = Literal["prometheus", "zabbix", "azure_kql", "dynatrace", "checkmk", "generic"]


class NativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: NativeDialect
    label: Literal["Illustrative draft"] = "Illustrative draft"
    text: str


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: RuleKind
    title: str
    reasoning: str
    rule: Rule
    description: str = ""                     # deterministic plain English (rules.describe)
    backtest: Optional[BacktestResult] = None # computed by the engine, never by the model
    native_draft: Optional[NativeDraft] = None
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    origin: Literal["live", "saved"] = "live"
    active_rule_id: Optional[str] = None      # set once accepted
