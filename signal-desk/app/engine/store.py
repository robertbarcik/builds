"""Active rules: the seed rules ("what the desk already has today") plus accepted ones, each
with an on/off switch, persisted as JSON under data/state/ (override the folder with
SIGNAL_DESK_STATE_DIR, the tests do)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from app.config import DATA_DIR
from app.engine.rules import RuleSpec, rule_to_dict, validate
from app.models import ActiveRule, Rule

STATE_FILE = "rules.json"

# What a typical desk already filters today: audit chatter and duplicate notices. Deliberately
# modest, so plenty of real noise (NTP flaps, test VM, stale hosts, tight thresholds) is left
# for the rule engineer to find.
SEED_RULES: list[dict[str, Any]] = [
    {
        "id": "seed-fw-admin-logins",
        "title": "Firewall admin logins",
        "rule": {"kind": "suppress", "match": {"source": "fortigate", "title_contains": "Admin login successful",
                                                "severity_in": ["info"]},
                 "window_min": 60, "max_per_window": 0},
    },
    {
        "id": "seed-advisor-daily",
        "title": "Azure Advisor once a day",
        "rule": {"kind": "suppress", "match": {"source": "azure_monitor", "title_contains": "Advisor:",
                                                "severity_in": ["info"]},
                 "window_min": 1440, "max_per_window": 1},
    },
    {
        "id": "seed-backup-success",
        "title": "Backup success notices",
        "rule": {"kind": "suppress", "match": {"source": "veeam", "title_contains": "finished with Success",
                                                "severity_in": ["info"]},
                 "window_min": 60, "max_per_window": 0},
    },
]


def state_dir() -> Path:
    env = os.environ.get("SIGNAL_DESK_STATE_DIR", "").strip()
    return Path(env) if env else DATA_DIR / "state"


def seed_rules() -> list[ActiveRule]:
    out = []
    for s in SEED_RULES:
        rule, errs = validate(s["rule"])
        if errs:                                  # a broken seed is a programming error
            raise ValueError(f"seed rule {s['id']}: {errs}")
        out.append(ActiveRule(id=s["id"], title=s["title"], rule=rule, enabled=True))
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:28] or "rule"


class RuleStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else state_dir() / STATE_FILE
        self.rules: list[ActiveRule] = []
        self.load()

    # ---------------------------------------------------------- persistence
    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.rules = [ActiveRule.model_validate(r) for r in data.get("rules", [])]
                return
            except Exception:
                pass                              # unreadable state: start from the seeds
        self.rules = seed_rules()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "rules": [r.model_dump(by_alias=True, exclude_none=True) for r in self.rules]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".rules-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)

    def reset_to_seed(self) -> None:
        self.rules = seed_rules()
        self.save()

    # ---------------------------------------------------------- queries
    def get(self, rid: str) -> Optional[ActiveRule]:
        return next((r for r in self.rules if r.id == rid), None)

    def enabled(self) -> list[ActiveRule]:
        return [r for r in self.rules if r.enabled]

    def specs(self) -> list[RuleSpec]:
        """(id, title, rule) of the enabled rules, in order: what the engine runs."""
        return [(r.id, r.title, r.rule) for r in self.rules if r.enabled]

    # ---------------------------------------------------------- changes
    def set_enabled(self, rid: str, enabled: bool) -> ActiveRule:
        r = self.get(rid)
        if r is None:
            raise KeyError(rid)
        r.enabled = enabled
        self.save()
        return r

    def toggle(self, rid: str) -> ActiveRule:
        r = self.get(rid)
        if r is None:
            raise KeyError(rid)
        return self.set_enabled(rid, not r.enabled)

    def add(self, rule: Union[Rule, dict], title: str, *, proposal_id: Optional[str] = None,
            live_since_ts: Optional[str] = None) -> ActiveRule:
        """Validate and append a rule (enabled). Raises ValueError with the error strings."""
        parsed, errs = validate(rule if isinstance(rule, dict) else rule_to_dict(rule))
        if errs:
            raise ValueError("; ".join(errs))
        title = (title or "").strip()[:80] or "New rule"
        base = f"r-{_slug(title)}"
        rid, n = base, 2
        while self.get(rid):
            rid, n = f"{base}-{n}", n + 1
        ar = ActiveRule(id=rid, title=title, rule=parsed, enabled=True, live_since_ts=live_since_ts,
                        proposal_id=proposal_id)
        self.rules.append(ar)
        self.save()
        return ar


_STORE: Optional[RuleStore] = None


def get_store() -> RuleStore:
    """The process-wide store (created on first use)."""
    global _STORE
    if _STORE is None:
        _STORE = RuleStore()
    return _STORE
