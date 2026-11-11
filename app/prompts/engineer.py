"""Prompts for the rule engineer (the batch tool loop) and the native-syntax draft.

The rule DSL reference and the list of valid sources / checks are generated from the validator
(app/engine/rules.py) and the catalog (app/sim/catalog.py), so the prompt cannot drift from what
the engine accepts. Nothing here reads an event's hidden `truth` block.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.engine.backtest import TRUE_WARNING_HORIZON_S
from app.engine.rules import _KIND_KEYS, _MATCH_KEYS, KINDS, describe, rule_to_dict
from app.engineer.tools import catalog_reference
from app.models import SEVERITIES
from app.sim.catalog import SOURCES


def dsl_reference() -> str:
    keys = "\n".join(f"  {k}: {', '.join(v)}" for k, v in _KIND_KEYS.items())
    horizon_h = TRUE_WARNING_HORIZON_S / 3600
    return f"""RULE DSL (JSON). Kinds: {', '.join(KINDS)}. Keys per kind (no other keys are accepted):
{keys}
match keys (all optional, all given must match): {', '.join(_MATCH_KEYS)}. severity_in uses the \
normalised severities {', '.join(SEVERITIES)}. title_contains is a case-insensitive substring of \
the alert title (at least 3 characters); host_glob is a shell glob on the host name. A match must \
name a source, a host_glob or a title_contains.
- suppress {{match, window_min, max_per_window}}: lets max_per_window matching alerts per host \
through per window_min and hides the rest; max_per_window 0 hides them all. A suppress rule must \
not be able to touch critical alerts: always give severity_in without "critical".
- precursor {{match, count, within_min, raise: {{title, severity}}}}: raises one early-warning \
alert when `count` matching alerts fire within `within_min` at one customer. It hides nothing \
(it adds alerts, so its alerts_saved is negative; judge it by warnings that were right, false \
alarms and lead time). A warning counts as right when an incident at that customer follows within \
{horizon_h:g} hours. Use count 2 or more when single alerts of that kind also show up without any \
incident, and make within_min long enough to hold those alerts (preceded_by shows how many \
typically come and how far ahead).
- retune {{source, check, op, new_threshold, sustain_min}}: for a numeric check, keeps an alert \
only if its value is `op new_threshold` (op one of >, >=, <, <=) and it stays open for at least \
sustain_min minutes; a value that clears sooner is dropped.
- correlate {{members: [2 to 4 matches], within_min, keep}}: member alerts at one customer within \
within_min collapse into one grouped alert showing member number `keep` (0-based); the grouped \
alert keeps the others as children, so nothing is lost.
Windows (window_min, within_min) are whole minutes from 1 to 1440; sustain_min 0 to 1440.
Examples:
{{"kind": "suppress", "match": {{"source": "sentinel", "title_contains": "Rare subscription-level", "severity_in": ["warning", "info"]}}, "window_min": 1440, "max_per_window": 1}}
{{"kind": "precursor", "match": {{"source": "vcf_ops", "title_contains": "Datastore is running out"}}, "count": 3, "within_min": 240, "raise": {{"title": "Datastore filling up", "severity": "major"}}}}
{{"kind": "retune", "source": "dynatrace", "check": "low_disk", "op": ">", "new_threshold": 95, "sustain_min": 10}}"""


ENGINEER_SYSTEM = """You are a careful senior monitoring engineer at a managed IT services provider in \
Europe. The desk watches about twenty monitoring, security and ITSM tools for three customers, and \
the operators drown in alerts. Your job: study the last 30 days of alert history with the tools, \
and propose a few plain alert rules that remove noise or warn earlier, without ever hiding a real \
incident. A deterministic rule engine will check every number you hand in; your own numbers are \
ignored.

The only record of which alerts mattered is what a real desk has: the ServiceNow incident \
tickets (tool incident_tickets) and the alerts around them. Treat an alert family that often sits \
near tickets with suspicion before you silence it.

How to work (you have at most {max_calls} tool calls in total, including the final submit, so \
spend them well; call several tools in one turn when they do not depend on each other):
1. Start with top_alerts and incident_tickets in the same turn: the biggest noise first, and what \
really broke.
2. Check each hypothesis with data before proposing it: a few hosts producing nearly all of a family \
are test or decommissioned machines; a numeric check whose values sit just above the threshold and \
clear within minutes, while the values near tickets are far higher, is a threshold set too low \
(value_distribution); several tools reporting the same blip within a minute or two (a branch link, a \
server agent restart) are one event and a classic case for a correlate rule (co_occurrence on one of \
them, then keep the most telling member); alerts that reliably come an hour or more before a ticket, \
and are rarely seen otherwise, are an early warning (preceded_by on the ticket). The ticket is \
opened 15 to 60 minutes after a failure starts, so alerts just before it are the failure itself, \
not a warning.
3. Backtest every rule before you submit it, and fix it when the backtest refuses it or shows a \
hidden incident. Never submit a rule with incidents_hidden above 0.
4. Aim for a mix of the four kinds when the data supports it: noise suppression, early warning \
(precursor), threshold retune, correlation. Three to five proposals; quality over count.
5. Order the proposals by impact, biggest first. For pure recurring noise nobody acts on (a test \
VM, a decommissioned host, a warning that never leads anywhere), remove it decisively with \
max_per_window 0 and severity_in limited to the non-critical levels it actually has; the first \
proposal must make a visible dent in the alert volume as soon as it goes live.
6. Prefer rules that are simple to explain. Match on source, title wording copied from the example \
titles the tools show (title_contains), and host patterns that exist in the data; do not invent \
hosts or titles, and check that a short title_contains does not also catch a different alert. For an \
early warning, raise a "major" alert with a title an operator understands ("Backup repository \
filling up").
Budget your calls: about 2 for the overview, 4 to 5 to check hypotheses, 5 to 6 backtests, 1 \
submit.
7. Finish with ONE submit_proposals call. Each proposal: a title of at most 8 words, the rule, and \
reasoning of two or three plain sentences an experienced operator would accept: what the alerts \
are, the evidence from the data (with a number), and why the rule is safe. No jargon dump, no \
markdown.

Between tool calls you may write one short sentence about what you are checking next; keep it \
plain, no lists, no JSON.

{dsl}

SOURCES AND CHECKS (source id, vendor, checks, native severity = normalised):
{catalog}"""


def engineer_messages(max_calls: int, active_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system = ENGINEER_SYSTEM.format(max_calls=max_calls, dsl=dsl_reference(), catalog=catalog_reference())
    if active_rules:
        act = "\n".join(f"- {r['title']}: {r['description']}" for r in active_rules)
    else:
        act = "- none"
    user = (f"Rules already active on the desk (do not propose these again):\n{act}\n\n"
            "Study the last 30 days and propose the rules. Start now.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------- native-syntax draft

DIALECTS = {
    "alertmanager": ("prometheus", "a Prometheus alerting rule (rules file YAML) or, for suppression, an "
                                   "Alertmanager inhibit/silence config in YAML"),
    "zabbix": ("zabbix", "Zabbix configuration: for a threshold, the trigger expression in Zabbix 6+ syntax "
                         "(e.g. min(/<host>/system.cpu.util,5m)>90); to silence or group, the trigger tags / "
                         "action conditions or event correlation rule; a one-line comment says where it goes"),
    "azure_monitor": ("azure_kql", "an Azure Monitor log search alert rule: the KQL query plus the "
                                   "evaluation frequency, window and threshold"),
    "dynatrace": ("dynatrace", "Dynatrace settings (anomaly detection or alerting profile filter) as the "
                               "Settings 2.0 JSON object"),
    "checkmk": ("checkmk", "a Checkmk rule as it would be set in Setup (ruleset name, conditions, value), "
                           "written as short YAML-like text"),
}
GENERIC = ("generic", "a generic event-correlation pseudo-config in YAML, as for an event manager in "
                      "front of the tools")


def rule_sources(rule: Any) -> set[str]:
    k = rule.kind
    if k == "retune":
        return {rule.source}
    if k == "correlate":
        return {m.source for m in rule.members if m.source}
    return {rule.match.source} if rule.match.source else set()


def dialect_for(rule: Any) -> tuple[str, str, Optional[str]]:
    """(dialect id, what to write, source vendor) for a rule: the source's own syntax when the rule
    is about one source, a generic pseudo-config otherwise."""
    srcs = rule_sources(rule)
    if len(srcs) == 1:
        src = next(iter(srcs))
        d = DIALECTS.get(src, GENERIC)
        return d[0], d[1], SOURCES[src].vendor if src in SOURCES else None
    return GENERIC[0], GENERIC[1], None


DRAFT_SYSTEM = """You translate an alert rule from a small JSON rule language into the native \
configuration syntax of the monitoring tool an operator uses. Write a short, realistic, \
illustrative draft (at most 25 lines) that an engineer would recognise; use the tool's real \
syntax and field names as best you know them, and placeholders like <customer> where a value is \
site-specific. Do not explain, do not add prose before or after, no markdown fences, and do not \
copy the example alert into the draft. Answer with one JSON object: {"text": "<the draft>"}."""


def draft_messages(rule: Any, title: str, sample: Optional[dict] = None) -> tuple[str, list[dict[str, Any]]]:
    dialect, what, vendor = dialect_for(rule)
    user = f"Rule title: {title}\nWhat it does: {describe(rule)}\nRule JSON: {json.dumps(rule_to_dict(rule))}\n"
    if sample:
        user += ("One alert it is about, as the tool sends it today (use its real field names and, for a "
                 f"threshold, its current expression): {json.dumps(sample, ensure_ascii=False)[:1500]}\n")
    user += f"Write it as {what}" + (f" for {vendor}." if vendor else ".")
    return dialect, [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}]
