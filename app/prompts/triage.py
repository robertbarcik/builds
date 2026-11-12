"""Prompts for live triage: the structured grouping call and the streamed plain-language summary.

Only the fields listed in `event_line()` ever reach the model. The hidden `truth` block of an
event (scenario, role, pattern) is never read here; `assert_prompt_clean()` double-checks the
final messages before they are sent.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from app.models import Event

TRIAGE_SYSTEM = """You are the senior on-call operator at a managed IT services provider in Europe. \
Your desk watches about twenty monitoring, security and ITSM tools for three customers. Every \
8 seconds you get the alerts that got past the alert rules, plus the incidents already on the \
board. Fold the alerts into a handful of incidents a human should act on.

What you know from the CMDB (host names follow it):
- <prefix>-core-sw-NN are the core switches. Access switches (-acc-sw-NN), branch routers \
(-br-<city>-rt01), firewalls (-fw-NN) and all servers (-win-, -app-, -web-, -sql-, -db-, -k8s-, \
-batch-, -sap-, -esx-, -nas-, -vbr-) of that customer sit behind them.
- Veeam backup jobs write to the NetApp volume vol_veeam_repo01 through the -vbr- server; a full \
volume makes the jobs fail.
- The customer portal's TLS certificate is served by the web hosts and the Application Gateway; \
an expired certificate makes probes, synthetic monitors and logins fail.
- The host prefix tells the customer (see the customer list).

Procedure for each batch:
1. Look for a burst: many alerts for one customer within a few minutes. Find the EARLIEST or most \
upstream event in it (a core switch interface down, a link monitor down, a volume full, a \
certificate expired, a job ended not OK, a service stopped). That is the root event. Everything \
of that customer that fails right after it and depends on it (unreachable hosts, ping down, \
heartbeat failures, node down, failed jobs, failed probes, error rates, tickets about it) is a \
symptom: ONE incident, all their ids in it. Never split one burst into several incidents by \
tool, host type or site.
2. If an incident on the board already covers that cause, attach the alerts that cause explains \
(same customer, same chain of failure) instead of opening a new incident. Never attach routine \
noise to an incident just because it is the same customer. Incidents marked "acknowledged" are \
being handled: attach related alerts there, never reopen. When the new alerts show the impact \
has grown (many more hosts or a customer-facing service now down), add "severity" to the attach \
entry to raise it.
3. Everything else is usually noise. Do NOT open incidents for: single threshold blips (CPU, \
disk, memory, throttling), NTP offset warnings, a test VM or decommissioned host that keeps \
reporting, "unusual traffic", HPA at max replicas, failed logins, informational notices. Also \
noise: SCOM "Health Service Heartbeat Failure" or "Failed to Connect to Computer" on Windows \
servers when no OTHER tool (Checkmk, PRTG, Zabbix, Dynatrace) reports those servers down: that \
is the SCOM agent restarting and it clears by itself, even if it happens on a few servers in a \
row. Put those ids in noise_ids.
4. Lines marked [raised by rule] are early warnings from a rule the operators accepted: \
something is about to break (for example a backup repository filling up). Open ONE incident \
for it (major unless the line says otherwise) titled with what is at risk and where, unless an \
incident on the board already covers it; later alerts of the same chain attach to that incident.
5. A lone device down with nothing behind it failing (one branch router, one server) is at most \
ONE minor or major incident, never several. Two unrelated servers with the same symptom are not \
one incident.
6. Open at most two new incidents per batch; most batches open none.
Writing the card:
- title: the cause in at most 8 words, naming the root device or object ("Core uplink Te1/0/9 \
down on abc-core-sw-09"), not a symptom count.
- Severity is about impact, not the loudest tool's word: critical = a customer-facing service \
or many hosts are down, or data is at risk now; major = one site, device or service is broken \
but contained, or will break soon; minor = degraded, worth a look today. One branch router or \
one server down is major, not critical.
- likely_cause names a mechanism, not a restatement ("Te1/0/9 on abc-core-sw-09 went down, \
cutting off everything behind it", not "the hosts are unreachable because they do not respond"). \
If the lines do not show the mechanism, write "Not confirmed yet:" and the best lead. At most \
25 words.
- next_step is one concrete action in at most 30 words: which host or device to check first, \
what to check, and which team to call (Network Operations, Backup & Storage, Database Team, \
Platform Team, Batch Operations, Web Operations). For example "Check Te1/0/9 on abc-core-sw-09 \
(optics, cable, remote port); call Network Operations if it stays down."
- Never invent hosts, interfaces, jobs, numbers or facts that are not in the lines. Use only \
the event ids (e1, e2, ...) of the new alerts.

Answer with ONE compact JSON object and nothing else:
{"attach": [{"incident_id": "...", "event_ids": ["e3", "e7"], "severity": "<optional, only to raise>"}],
 "new": [{"customer": "<customer id>", "title": "...", "severity": "critical|major|minor",
          "likely_cause": "...", "next_step": "...", "event_ids": ["e1", "e2"]}],
 "noise_ids": ["e4"]}
An event id appears at most once. Empty lists are fine."""


SUMMARY_SYSTEM = """You write the summary on an incident card for the on-call operator and the \
customer's service manager. Two short sentences, plain English, no bullet points, no headings, \
no jargon dump. The card prints the likely cause on its own line right under your summary and \
the next step below that, so do NOT restate either of them. Your summary says what the people \
affected notice: which service or site is broken and for whom, how wide the impact is in words \
(which kinds of devices and services are affected), and where it stands now (still failing, \
spreading, clearing). Example: the likely cause says "Te1/1/1 on abc-core-sw-02 went down, \
cutting off everything behind it"; a good summary does not mention the interface again but says \
"Warehouse sites and the order portal at Nordhafen are offline, and staff there cannot reach \
email or ERP. Nothing has recovered yet." Do not state alert or tool counts: the card shows them and they keep \
growing. Use only facts from the card and the alert lines; never invent hosts, numbers or \
causes, and leave out any alert line that does not fit the card's cause. Never explain a symptom \
with itself ("unreachable because it does not respond"). Write the customer's short name, not \
the legal form."""


def customers_legend(customers: dict[str, Any]) -> str:
    return "; ".join(f"{c.id} = {c.name} ({c.industry}, {c.country}, hosts {c.prefix}-*)"
                     for c in customers.values())


def event_line(e: Event, ref: Optional[str] = None) -> str:
    """`id|ts|customer|source|host|severity|title`, time as HH:MM:SS. No other field is sent.
    `ref` replaces the event id with a short per-tick alias (fewer tokens in and out)."""
    title = e.title.replace("|", "/").replace("\n", " ")
    if e.produced_by:
        title = f"[raised by rule] {title}"
    return f"{ref or e.id}|{e.ts[11:19]}|{e.customer}|{e.source}|{e.host}|{e.severity}|{title}"


def incident_block(inc: dict[str, Any]) -> str:
    """One open incident for the prompt: id, title, customer, summary, latest evidence."""
    head = f"- {inc['id']} [{inc['status']}] {inc['customer']} | {inc['severity']} | {inc['title']}"
    body = inc.get("summary") or inc.get("likely_cause") or ""
    lines = [head]
    if body:
        lines.append(f"  summary: {body[:280]}")
    for ln in inc.get("latest", [])[:4]:
        lines.append(f"  evidence: {ln}")
    lines.append(f"  evidence so far: {inc.get('evidence_count', 0)} alerts")
    return "\n".join(lines)


def triage_messages(lines: list[str], incidents: list[dict[str, Any]], customers: dict[str, Any],
                    sim_time: str, omitted: int = 0) -> list[dict[str, str]]:
    board = "\n".join(incident_block(i) for i in incidents) if incidents else "(none)"
    more = f"\n({omitted} lower-severity alerts in this window were left out.)" if omitted else ""
    user = (f"Simulated time: {sim_time} UTC.\nCustomers: {customers_legend(customers)}.\n\n"
            f"Incidents on the board:\n{board}\n\n"
            f"New alerts (id|time|customer|source|host|severity|title):\n" + "\n".join(lines) + more)
    msgs = [{"role": "system", "content": TRIAGE_SYSTEM}, {"role": "user", "content": user}]
    assert_prompt_clean(msgs)
    return msgs


def summary_messages(card: dict[str, Any], lines: Iterable[str], customer_name: str,
                     previous: Optional[str] = None) -> list[dict[str, str]]:
    ev = "\n".join(lines)
    prev = f"\nPrevious summary (update it, the incident has grown): {previous}" if previous else ""
    user = (f"Customer: {customer_name}\nTitle: {card['title']}\nSeverity: {card['severity']}\n"
            f"Likely cause (already on the card, do not repeat it): {card.get('likely_cause') or 'not stated'}\n"
            f"Alerts on the card: {card.get('evidence_count', 0)} from {card.get('tool_count', 0)} tools"
            f"{prev}\nAlert lines (id|time|customer|source|host|severity|title):\n{ev}")
    msgs = [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]
    assert_prompt_clean(msgs)
    return msgs


class PromptLeak(RuntimeError):
    pass


def assert_prompt_clean(messages: list[dict[str, Any]]) -> None:
    """Ground truth must never reach a model."""
    blob = " ".join(str(m.get("content") or "") for m in messages)
    m = _LEAK.search(blob)
    if m:
        raise PromptLeak(f"prompt contains {m.group(0)!r}")


# what a leaked truth block would look like: its keys, or its pattern labels
_LEAK = re.compile(r"\btruth\b|scenario_id|\b(?:noise|tight|group|orphan|scenario):[a-z_]+")
