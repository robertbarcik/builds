# Signal Desk

A live demo of generative AI for an operations desk that watches 20-30 monitoring, security and
ITSM tools. **The LLM proposes, plain rules run.**

- **Live triage:** a stream of synthetic alerts from about twenty real tool vocabularies
  (Dynatrace, Zabbix, Checkmk, Azure Monitor, Prometheus Alertmanager, Splunk, Sentinel, Veeam,
  ServiceNow, ...) for three fictional customers. Every 8 seconds a fast, cheap model folds what
  got past the rules into a few incident cards a human can act on: title, a streamed summary,
  the likely cause, evidence, one next step.
- **The rule engineer:** a stronger model studies 30 days of alert history with seven tools and
  proposes three to five plain rules (suppress, early warning, threshold retune, correlate). A
  deterministic rule engine re-checks every proposal against the history. Accept one and the
  noise visibly drops.
- **Every number the visitor sees comes from the engine,** never from the model.

All data is synthetic. Vendor names are used for illustration only; no affiliation.

## Run it

```bash
./setup.sh                                  # Python 3.12 venv + pinned requirements
cp .env.example .env                        # put OPENROUTER_API_KEY in it
.venv/bin/python scripts/verify_models.py   # model IDs and prices against OpenRouter's live list
./run.sh                                    # http://127.0.0.1:7870 (Ctrl-C stops it)
./run.sh --detach                           # same, in the background; log in logs/server.log
```

Stop a detached server: `kill $(lsof -ti tcp:7870 -sTCP:LISTEN)`. Only the listener, never
`lsof -ti :7870` (that also lists the browser).

**Spend:** live triage costs about $0.5 an hour while a browser is open; it stops when no
browser is connected (the simulated clock pauses too). One rule-engineer run costs $0.15-0.4.
Caps in `config.yaml` (hourly, daily, per engineer session, cooldown) stop it calmly.

Tests: `.venv/bin/python -m pytest tests -q` (no network). End to end with real money (about
$0.2): `./run.sh --detach && .venv/bin/python scripts/live_check.py`.

## Five-minute run sheet for the customer meeting

Before the meeting: `./run.sh --detach`, open http://127.0.0.1:7870 in Chrome, then on the
**Proposals** tab click **load last run** once (proves the offline fallback works), then
**Reset demo**. Leave the page on **Now**. Keep `data/saved_run.json` in place.

| Time | They click | What they see | What you say |
|---|---|---|---|
| 0:00 | nothing | headline "N alerts came in → M need a human", the noise strip, raw alerts scrolling | "This is a desk watching twenty tools for three customers. Simulated time runs a minute per second. Most of this is noise your operators read today." |
| 0:45 | **Break something → Pull the core uplink** | within ~10 s a red card: "Interface Te1/1/1 down on ...-core-sw-..", summary typing in | "One cable. Eight tools fire dozens of alerts. The AI folds them into one card: what is broken, why, what to do first. Open the evidence: those are the raw lines from each tool." |
| 1:45 | **Got it** on the card | the card leaves; "need a human" drops | "A human acknowledges; the AI keeps attaching related alerts silently." |
| 2:00 | **Proposals → Ask AI to study 30 days of history** | a work log: tool calls, backtests; after ~30 s, 3-5 proposal cards | "Now the second use: an AI engineer reads a month of history and proposes rules. Each card's numbers come from our rule engine replaying the month, not from the AI. Look: 'incidents still caught 42 of 42'." Point at the cost line under the summary: "that run cost about fifteen cents, once." |
| 3:15 | open **In your tool's syntax** on one card | an illustrative Zabbix / Checkmk / KQL draft | "It also drafts the rule for the tool you run. A draft: your engineer checks it." |
| 3:30 | **Accept** on the first card | jumps to Now; a marker on the strip; after a few seconds: "Alerts reaching a human since ...: X/min without it → Y/min with it (−Z%)", about −15 to −22 % for any one noise rule; accept two or three and the bars roughly halve | "That rule is plain code now, no AI in the loop. Same minutes, with and without the rule: that is the noise your people no longer read." |
| 4:15 | **Active rules** | the list with on/off switches and live counts | "Every rule can be switched off. Nothing the AI suggested runs without a human accepting it." |
| 4:40 | (optional) the early-warning card | "Early warning for expiring certificates", warnings right, lead ~1.5 days | "Not only less noise: warnings a day or two before the outage." |
| 5:00 | **Reset demo** | clean board | |

**If the network or the model fails:**

- *Triage fails or is slow:* after two failed calls the chip "AI paused, rule-based grouping"
  appears and cards keep coming from deterministic grouping (labelled "Rule-based"). Say: "this
  is the fallback; the desk never goes blind." It probes the AI every 30 s and resumes on its own.
- *Engineer fails, times out or the network is gone:* the Proposals tab says so and offers
  **Load the last run (saved DATE)**. It replays the saved run offline and recomputes all numbers
  against today's active rules. Say "this is yesterday's run, re-checked now."
- *Spend cap reached:* same two fallbacks; the chip says "spend cap reached".
- *Page stuck or blank:* reload the browser tab; the server keeps its state. Still stuck:
  `./run.sh --detach` restarts it (fresh desk, saved run intact).
- *No internet at all:* fonts and Alpine.js come from CDNs; have the page loaded once before
  the meeting (the browser cache keeps them) or tether to a phone.

## How it fits together

`app/sim/` generates the stream and the 30-day history (seeded, deterministic); `app/engine/`
validates and runs rules and backtests them; `app/triage/loop.py` is LLM use 1;
`app/engineer/` is LLM use 2; `app/llm/` holds the OpenRouter client and the budget; the whole
UI is `static/index.html` (Alpine.js, no build). Details and invariants: `CLAUDE.md`. Public
deploy (prepared, not run): `deploy/README.md`.
