# Signal Desk — notes for Claude

GenAI alert-triage demo for a customer meeting (an EU IT services company). A person from the
customer sits at the keyboard: polish and robustness beat features. Concept: **the LLM proposes,
plain rules run.** Overview and the five-minute run sheet: `README.md`. Plan of record:
`~/.claude/plans/okay-so-replying-to-crystalline-sifakis.md`. Local git only (branch `main`, no
remote); do not create a GitHub repo or push unless Robert asks.

## Layout

```
config.yaml           models per role, prices, caps (+ `public.caps`), triage aging, sim settings
run.sh / setup.sh     start (127.0.0.1:7870, boot-id health check, kills only the port listener) / venv
app/main.py           FastAPI routes, SSE /api/stream, calm 500 handler, wiring
app/auth.py           cookie login form, only when LOGIN_ENABLED=1 (deploy); local runs open
app/config.py         config + secrets (env_value, openrouter_key), public_mode, dev_rules_enabled
app/sim/              catalog (vendors, templates), scenarios (8), generator (history), live stream,
                      clock; live.py also owns the noise strip and the drop figure
app/engine/           rules.py (DSL validate/describe/evaluate), backtest.py, store.py, runtime.py
app/triage/loop.py    LLM use 1: batch -> cards, summaries, aging, degraded mode
app/engineer/         tools.py (7 analysis tools + backtest), agent.py (tool loop, server-side check,
                      drafts, saved run, scrub_numeric_claims)
app/llm/              client.py (OpenRouter via openai SDK, loose JSON, streaming), budget.py (caps)
app/prompts/          triage + engineer prompts; DSL reference generated from the validator
static/index.html     the whole UI (Alpine.js 3.17.4 from CDN), riso.css (Riso rules below)
data/history.jsonl    generated (gitignored); data/saved_run.json = last good engineer run (tracked)
scripts/              build_history, verify_models, try_rule, live_check
deploy/               EC2 kit, PREPARED NOT RUN (deploy/README.md)
```

## Invariants (tests guard each; keep them)

- **Truth never leaves the server.** Events carry a hidden `truth` block (scenario, role,
  pattern). Only `app/engine/backtest.py` reads it. Never in prompts (`assert_prompt_clean`),
  agent tool output (`tools.assert_clean`), SSE (`format_sse` raises on a `truth` key) or any API
  response (`tests/test_api.py::test_no_api_response_carries_ground_truth`). A new endpoint or
  payload gets a line in that test.
- **Numbers only from the engine.** Backtest stats, the engineer's summary line and the noise-
  drop figure are computed by the engine. The model's own numbers are ignored; reasoning
  sentences that quote counts or shares are removed (`scrub_numeric_claims`), and the prompt
  asks for none.
- **The key is never logged, printed or returned.** `/api/health` says only `key_present`.
  Never `cat .env`, never commit it. Same for APP_PASSWORD / ADMIN_PASSWORD.
- **Idle spends nothing:** no browser connected -> the sim clock pauses and triage skips.
- **A cap is never an error page:** triage degrades to rule-based grouping (chip says why), the
  engineer offers the saved run; unhandled errors return `{"ok": false, "error": "server_error"}`.
- `POST /api/rules` (manual rules) is a dev aid: off unless `dev.manual_rules` or
  `SIGNAL_DESK_DEV=1`, and always off with `SIGNAL_DESK_PUBLIC=1`.

## Behaviour worth knowing

- Precursor rules may set `horizon_h` (1-72, default 6): the backtest credits a warning when the
  incident its triggering alerts belong to starts within that window (cert expiry warns 1-2 days
  ahead). Credit needs that causal link, so false alarms stay false at any horizon.
- Triage cards: a card whose alerts have all resolved goes quiet after `recovered_quiet_min`;
  a new incident whose alerts already all resolved (branch-router blip) opens no card. Summary
  sentences that restate the likely cause are dropped (`drop_repeated_cause`).
- Cost readout: "per minute" on Now = session-less calls (live triage) only; the engineer run's
  one-off cost is `engineer.cost_usd`, shown under the run summary.
- Noise-drop figure (`LiveSim.drop_payload`): since the last accepted rule, firing alerts per
  minute reaching a human with the rule vs that plus what the rule removed (same minutes).
  A before/after comparison was tried first and was too noisy and skewed by scenario resolves.

## Models

IDs and prices live in `config.yaml` and are verified against OpenRouter's live list with
`.venv/bin/python scripts/verify_models.py` (never from memory). Triage and render:
`openai/gpt-4.1-mini`; engineer: `anthropic/claude-sonnet-5.5`. A **forced `tool_choice`
returns HTTP 400 on the engineer model** via OpenRouter: the loop always sends `"auto"` and asks
for `submit_proposals` in words when the budget or time runs out (`FORCE_SUBMIT_TEXT`).

## Run, test, check

```bash
.venv/bin/python -m pytest tests -q          # offline, ~8 s
./run.sh --detach                             # then stop: kill only the listener on 7870
.venv/bin/python scripts/live_check.py        # real money (~$0.2): boot id, inject, AI card,
                                              # engineer run, accept, drop; PASS/FAIL table
.venv/bin/python scripts/try_rule.py examples/rules/checkmk_ntp_flaps.json   # backtest a rule file
```

A live check run with a good engineer result rewrites `data/saved_run.json` (tracked): review
the diff before committing it. Always stop the server when done: an open browser stream spends.

## UI rules (Riso, `training-ops/web/riso/DEMO_RESKIN.md`)

Square corners, sentence-case labels, navy ink, red/amber/green only for severity (blue, pink,
yellow are decoration). All visible wording sits in the `copy` object at the top of
`static/index.html`. No new charts; the plan deliberately leaves out settings, search, filters
and manual rule editing.
