"""Developer tool: validate a rule file, print its backtest against the 30-day history, and
optionally put it live on the running server.

Usage:
  .venv/bin/python scripts/try_rule.py examples/rules/checkmk_ntp_flaps.json [more.json ...]
  .venv/bin/python scripts/try_rule.py examples/rules/checkmk_ntp_flaps.json --activate
  options: --no-active (backtest without the seed / active rules), --url http://127.0.0.1:7870

A rule file is either {"title": "...", "rule": {...}} or a bare rule object. The backtest runs
on top of the seed rules (or, with --server-rules, the enabled rules of the running server).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.engine.backtest import RuleInvalid, backtest, load_history  # noqa: E402
from app.engine.rules import describe, validate  # noqa: E402
from app.engine.store import seed_rules  # noqa: E402


def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    cfg = load_config()["server"]
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--activate", action="store_true", help="post the rule(s) to the running server")
    ap.add_argument("--no-active", action="store_true", help="backtest without the seed rules")
    ap.add_argument("--server-rules", action="store_true", help="backtest on top of the server's enabled rules")
    ap.add_argument("--url", default=f"http://{cfg['host']}:{cfg['port']}")
    args = ap.parse_args()

    active = [] if args.no_active else seed_rules()
    if args.server_rules:
        code, d = _http("GET", f"{args.url}/api/rules")
        if code != 200:
            print(f"cannot read rules from {args.url} (HTTP {code})")
            return 2
        active = [r["rule"] for r in d["rules"] if r["enabled"]]

    t0 = time.time()
    load_history()
    print(f"history loaded in {time.time() - t0:.2f}s; backtests on top of {len(active)} active rule(s)\n")

    rc = 0
    for f in args.files:
        doc = json.loads(Path(f).read_text())
        title = doc.get("title") if "rule" in doc else Path(f).stem.replace("_", " ")
        rule_d = doc["rule"] if "rule" in doc else doc
        print(f"== {f}: {title}")
        rule, errs = validate(rule_d)
        if errs:
            rc = 1
            print("   INVALID:")
            for e in errs:
                print(f"   - {e}")
            print()
            continue
        print(f"   {describe(rule)}")
        t = time.time()
        try:
            bt = backtest(rule_d, active)
        except RuleInvalid as e:
            print(f"   active rules invalid: {e.errors}")
            return 2
        dt = time.time() - t
        print(f"   alerts {bt.alerts_before} -> {bt.alerts_after}  saved {bt.alerts_saved} ({bt.saved_pct}%)")
        print(f"   incidents {bt.incidents_total} total, {bt.incidents_visible} visible, {bt.incidents_hidden} hidden")
        if rule.kind == "precursor":
            print(f"   early warnings: {bt.precursor_fired} fired, {bt.precursor_true} true, "
                  f"{bt.precursor_false} false alarms, median lead {bt.median_lead_min} min")
        for s in bt.samples:
            print(f"     | {s}")
        print(f"   backtest took {dt:.2f}s")
        if args.activate:
            code, d = _http("POST", f"{args.url}/api/rules", {"title": title, "rule": rule_d})
            if code == 200:
                print(f"   ACTIVATED on {args.url} as {d['rule']['id']}")
            else:
                rc = 1
                print(f"   activation failed (HTTP {code}): {d}")
        print()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
