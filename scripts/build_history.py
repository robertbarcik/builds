"""Build data/history.jsonl: 30 days of synthetic alerts with hidden ground truth.

Usage: .venv/bin/python scripts/build_history.py [--seed N] [--days 30] [--out data/history.jsonl]
Prints size, per-scenario occurrence counts and the noisiest sources.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import HISTORY_PATH, load_config  # noqa: E402
from app.sim.generator import generate_history  # noqa: E402


def main() -> int:
    cfg = load_config()["sim"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=cfg["seed"])
    ap.add_argument("--days", type=int, default=cfg["history_days"])
    ap.add_argument("--end", default=cfg["history_end"])
    ap.add_argument("--out", default=str(HISTORY_PATH))
    args = ap.parse_args()

    t0 = time.time()
    events, summary = generate_history(args.seed, args.end, args.days)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with out.open("w") as fh:
        for e in events:
            line = json.dumps(e, separators=(",", ":"), sort_keys=True)
            h.update(line.encode())
            fh.write(line + "\n")

    print(f"wrote {out} in {time.time() - t0:.1f}s")
    print(f"events: {len(events)}  ({len(events) / args.days:.0f}/day)  {summary['start']} .. {summary['end']}")
    print(f"sha256: {h.hexdigest()[:16]}  size: {out.stat().st_size / 1e6:.1f} MB")
    print(f"patch nights: {summary['patch_nights']}")
    print("scenario occurrences:", json.dumps(summary["scenarios"]))
    print("orphan precursor runs:", json.dumps(summary["orphans"]))
    roles = Counter(e["truth"]["role"] for e in events)
    print("truth roles:", dict(roles))
    pats = Counter((e["truth"]["pattern"] or "?").split(":")[0] for e in events)
    print("pattern families:", dict(pats))
    print("top patterns:")
    for p, n in Counter(e["truth"]["pattern"] for e in events).most_common(12):
        print(f"  {n:6d}  {p}")
    print("by source:")
    for s, n in Counter(e["vendor"] for e in events).most_common():
        print(f"  {n:6d}  {s}")
    print("by customer:", dict(Counter(e["customer"] for e in events)))
    print("by severity:", dict(Counter(e["severity"] for e in events)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
