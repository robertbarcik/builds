"""Server fleet tools — the demo's "actions with consequences".

A small simulated fleet in SQLite. Two risk tiers, decided in CODE (not in the
prompt):

  direct          list_servers, provision_server(size="small")
  human approval  reboot_server, terminate_server, migrate_server,
                  provision_server(size="medium"|"large")

The tier of a call is computed by APPROVAL_POLICY below from the tool name AND
its arguments; app/approvals.py holds the call until a human clicks Approve.
The handlers themselves know nothing about approval — they are only reached
once the gate has let the call through.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import DATABASE_PATH

SITES = ("frankfurt", "vienna", "bratislava")
SIZES = {
    "small":  {"spec": "2 vCPU / 4 GB",  "monthly_eur": 19},
    "medium": {"spec": "4 vCPU / 16 GB", "monthly_eur": 69},
    "large":  {"spec": "8 vCPU / 32 GB", "monthly_eur": 199},
}
ROLES = ("app", "db", "mail", "file", "gateway", "test")

# How long the simulated operations keep a server busy (seconds).
REBOOT_S, MIGRATE_S, PROVISION_S = 20, 45, 30

_SEED = [
    ("srv-app-01",  "app",     "medium", "frankfurt"),
    ("srv-app-02",  "app",     "medium", "frankfurt"),
    ("srv-db-01",   "db",      "large",  "frankfurt"),
    ("srv-mail-01", "mail",    "medium", "vienna"),
    ("srv-file-01", "file",    "small",  "bratislava"),
    ("vpn-gw-01",   "gateway", "small",  "bratislava"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    size TEXT NOT NULL,
    site TEXT NOT NULL,
    status TEXT NOT NULL,
    busy_until TEXT,
    created_at TEXT NOT NULL,
    last_action TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn() -> sqlite3.Connection:
    """Connection with the fleet table present, seeded and settled."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 0:
        _seed(conn)
    # Reboots / migrations / provisioning finish on their own.
    conn.execute(
        "UPDATE servers SET status = 'running', busy_until = NULL "
        "WHERE status IN ('rebooting', 'migrating', 'provisioning') AND busy_until <= ?",
        (_now().isoformat(),),
    )
    conn.commit()
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    created = (_now() - timedelta(days=90)).isoformat()
    conn.executemany(
        "INSERT INTO servers (name, role, size, site, status, created_at) VALUES (?, ?, ?, ?, 'running', ?)",
        [(n, r, s, site, created) for n, r, s, site in _SEED],
    )
    conn.commit()


def reset_fleet() -> str:
    """Back to the six seeded servers (demo re-runs)."""
    conn = _conn()
    try:
        conn.execute("DELETE FROM servers")
        _seed(conn)
    finally:
        conn.close()
    return list_servers()


def _norm_name(name: str) -> str:
    """'SRV DB 01', 'srv_db_1' → 'srv-db-01' (speech transcripts are loose)."""
    s = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    m = re.match(r"^(.*?)-?(\d+)$", s)
    if m:
        s = f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def _find(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM servers WHERE name = ? AND status != 'terminated'", (_norm_name(name),)
    ).fetchone()


def _row(r: sqlite3.Row) -> dict:
    return {
        "name": r["name"], "role": r["role"], "size": r["size"],
        "spec": SIZES.get(r["size"], {}).get("spec", ""), "site": r["site"],
        "status": r["status"], "monthly_cost_eur": SIZES.get(r["size"], {}).get("monthly_eur"),
    }


def _unknown(conn: sqlite3.Connection, name: str) -> dict:
    names = [r["name"] for r in conn.execute("SELECT name FROM servers WHERE status != 'terminated' ORDER BY name")]
    return {"error": f"Server '{name}' not found. Existing servers: {', '.join(names)}."}


# ── Handlers ─────────────────────────────────────────────────────────


def list_servers(status: str = "active") -> str:
    """The fleet. status: active (default, everything not terminated) or all."""
    conn = _conn()
    try:
        q = "SELECT * FROM servers" + ("" if (status or "").lower() == "all" else " WHERE status != 'terminated'")
        rows = [_row(r) for r in conn.execute(q + " ORDER BY name")]
    finally:
        conn.close()
    return json.dumps({
        "total": len(rows),
        "monthly_cost_eur": sum(r["monthly_cost_eur"] or 0 for r in rows if r["status"] != "terminated"),
        "servers": rows,
    })


def provision_server(role: str = "app", size: str = "small", site: str = "frankfurt") -> str:
    role, size, site = (role or "app").lower(), (size or "small").lower(), (site or "frankfurt").lower()
    if size not in SIZES:
        return json.dumps({"error": f"Unknown size '{size}'. Sizes: {', '.join(SIZES)}."})
    if site not in SITES:
        return json.dumps({"error": f"Unknown site '{site}'. Sites: {', '.join(SITES)}."})
    if role not in ROLES:
        role = "app"
    conn = _conn()
    try:
        prefix = f"srv-{role}-"
        taken = {r["name"] for r in conn.execute("SELECT name FROM servers WHERE name LIKE ?", (prefix + "%",))}
        n = 1
        while f"{prefix}{n:02d}" in taken:
            n += 1
        name = f"{prefix}{n:02d}"
        conn.execute(
            "INSERT INTO servers (name, role, size, site, status, busy_until, created_at, last_action) "
            "VALUES (?, ?, ?, ?, 'provisioning', ?, ?, 'provisioned')",
            (name, role, size, site, (_now() + timedelta(seconds=PROVISION_S)).isoformat(), _now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return json.dumps({
        "status": "executed", "action": "provision", "name": name, "role": role, "size": size,
        "spec": SIZES[size]["spec"], "site": site, "monthly_cost_eur": SIZES[size]["monthly_eur"],
        "server_status": "provisioning",
        "message": f"Server {name} ({size}, {site}) is being provisioned; ready in about {PROVISION_S} seconds.",
    })


def reboot_server(server_name: str, reason: str = "") -> str:
    conn = _conn()
    try:
        r = _find(conn, server_name)
        if not r:
            return json.dumps(_unknown(conn, server_name))
        conn.execute(
            "UPDATE servers SET status = 'rebooting', busy_until = ?, last_action = ? WHERE name = ?",
            ((_now() + timedelta(seconds=REBOOT_S)).isoformat(), f"reboot: {reason}"[:300], r["name"]),
        )
        conn.commit()
    finally:
        conn.close()
    return json.dumps({
        "status": "executed", "action": "reboot", "name": r["name"], "site": r["site"],
        "server_status": "rebooting", "reason": reason,
        "message": f"Server {r['name']} is rebooting; expected back in about {REBOOT_S} seconds.",
    })


def terminate_server(server_name: str, reason: str = "") -> str:
    conn = _conn()
    try:
        r = _find(conn, server_name)
        if not r:
            return json.dumps(_unknown(conn, server_name))
        conn.execute(
            "UPDATE servers SET status = 'terminated', busy_until = NULL, last_action = ? WHERE name = ?",
            (f"terminate: {reason}"[:300], r["name"]),
        )
        conn.commit()
    finally:
        conn.close()
    return json.dumps({
        "status": "executed", "action": "terminate", "name": r["name"], "site": r["site"],
        "server_status": "terminated", "reason": reason,
        "message": f"Server {r['name']} has been terminated and removed from the fleet.",
    })


def migrate_server(server_name: str, target_site: str, reason: str = "") -> str:
    target = (target_site or "").lower().strip()
    conn = _conn()
    try:
        r = _find(conn, server_name)
        if not r:
            return json.dumps(_unknown(conn, server_name))
        if target not in SITES:
            return json.dumps({"error": f"Unknown site '{target_site}'. Sites: {', '.join(SITES)}."})
        if target == r["site"]:
            return json.dumps({"error": f"Server {r['name']} is already in {target}."})
        conn.execute(
            "UPDATE servers SET status = 'migrating', site = ?, busy_until = ?, last_action = ? WHERE name = ?",
            (target, (_now() + timedelta(seconds=MIGRATE_S)).isoformat(),
             f"migrate {r['site']} -> {target}: {reason}"[:300], r["name"]),
        )
        conn.commit()
    finally:
        conn.close()
    return json.dumps({
        "status": "executed", "action": "migrate", "name": r["name"],
        "from_site": r["site"], "site": target, "server_status": "migrating", "reason": reason,
        "message": f"Server {r['name']} is being moved from {r['site']} to {target}; done in about {MIGRATE_S} seconds.",
    })


# ── Risk policy: which calls need a human, and what the human is shown ──
#
# Each entry maps a tool to fn(args) -> None | {"error": ...} | card
#   None            → low risk, run directly
#   {"error": ...}  → invalid call, answer the model without bothering a human
#   card            → shown in the confirmation window:
#                     {action, target, details: [[label, value], ...], impact}


def _server_card(action: str, impact: str):
    def policy(args: dict):
        conn = _conn()
        try:
            r = _find(conn, args.get("server_name", ""))
            if not r:
                return _unknown(conn, args.get("server_name", ""))
            details = [
                ["server", f"{r['name']} · {r['role']} · {r['size']} ({SIZES.get(r['size'], {}).get('spec', '')})"],
                ["site", r["site"]],
                ["status", r["status"]],
            ]
            if action == "migrate":
                target = (args.get("target_site") or "").lower().strip()
                if target not in SITES:
                    return {"error": f"Unknown site '{args.get('target_site')}'. Sites: {', '.join(SITES)}."}
                if target == r["site"]:
                    return {"error": f"Server {r['name']} is already in {target}."}
                details.append(["move to", target])
            if args.get("reason"):
                details.append(["reason", str(args["reason"])[:200]])
            return {"action": action, "target": r["name"], "details": details, "impact": impact}
        finally:
            conn.close()
    return policy


def _provision_policy(args: dict):
    size = (args.get("size") or "small").lower()
    if size == "small" or size not in SIZES:
        return None   # small: the agent may buy it on its own; unknown size: the handler answers
    site = (args.get("site") or "frankfurt").lower()
    return {
        "action": "provision",
        "target": f"new {size} server",
        "details": [
            ["size", f"{size} ({SIZES[size]['spec']})"],
            ["role", (args.get("role") or "app").lower()],
            ["site", site],
            ["cost", f"{SIZES[size]['monthly_eur']} EUR per month"],
        ],
        "impact": "Recurring cost above the agent's own spending limit (small servers only).",
    }


APPROVAL_POLICY = {
    "reboot_server": _server_card("reboot", f"Service on this server is interrupted for about {REBOOT_S} seconds."),
    "terminate_server": _server_card("terminate", "Irreversible. The server and its local data are removed."),
    "migrate_server": _server_card("migrate", f"The server is unavailable for about {MIGRATE_S} seconds while it moves."),
    "provision_server": _provision_policy,
}
