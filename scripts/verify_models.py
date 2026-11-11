"""Check every model ID in config.yaml against OpenRouter's live model list.

Prints ID, context length and live price per role and compares the price with config.yaml.
Exit code 1 if any configured ID is missing from the live list (or the list cannot be read).
The models endpoint is public; no API key is sent.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402

URL = "https://openrouter.ai/api/v1/models"


def main() -> int:
    cfg = load_config()
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "signal-desk-verify/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            live = {m["id"]: m for m in json.load(r)["data"]}
    except Exception as e:  # network down, API change
        print(f"could not read {URL}: {e}")
        return 1
    print(f"OpenRouter lists {len(live)} models\n")
    print(f"{'role':9} {'model':34} {'context':>9} {'$/M in':>8} {'$/M out':>8}  check")
    missing = 0
    for role, spec in cfg["models"].items():
        mid = spec["id"]
        m = live.get(mid)
        if not m:
            print(f"{role:9} {mid:34} {'-':>9} {'-':>8} {'-':>8}  MISSING")
            missing += 1
            continue
        p_in = float(m["pricing"]["prompt"]) * 1e6
        p_out = float(m["pricing"]["completion"]) * 1e6
        conf = cfg.get("prices", {}).get(mid, {})
        note = "ok"
        if conf and (abs(conf.get("input", -1) - p_in) > 1e-6 or abs(conf.get("output", -1) - p_out) > 1e-6):
            note = f"ok (config price {conf.get('input')}/{conf.get('output')} differs: update config.yaml)"
        elif not conf:
            note = "ok (no price in config.yaml)"
        params = set(m.get("supported_parameters") or [])
        if role == "engineer" and "tools" not in params:
            note += ", WARNING: no tool support listed"
        if role in ("triage", "render") and "response_format" not in params:
            note += ", WARNING: no response_format listed"
        print(f"{role:9} {mid:34} {m.get('context_length', 0):>9} {p_in:>8.3f} {p_out:>8.3f}  {note}")
    if missing:
        print(f"\n{missing} configured model(s) missing from the live list")
        return 1
    print("\nall configured models are live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
