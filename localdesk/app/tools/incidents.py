"""Incident management tool implementations."""

import json
import sqlite3
from datetime import datetime, timezone

from app.config import DATABASE_PATH


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DATABASE_PATH)


def create_incident(summary: str, priority: str, category: str, reporter_name: str) -> str:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO incidents (summary, priority, category, reporter_name, status, created_at)
               VALUES (?, ?, ?, ?, 'open', ?)""",
            (summary, priority.lower(), category.lower(), reporter_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        ticket_id = f"INC-{cur.lastrowid:05d}"
        cur.execute("UPDATE incidents SET ticket_id = ? WHERE id = ?", (ticket_id, cur.lastrowid))
        conn.commit()
        return json.dumps(
            {
                "ticket_id": ticket_id,
                "summary": summary,
                "priority": priority.title(),
                "category": category.title(),
                "reporter": reporter_name,
                "status": "Open",
                "message": f"Incident {ticket_id} created successfully.",
            }
        )
    finally:
        conn.close()


def list_incidents(status: str = "open", limit: int = 10) -> str:
    """Open / escalated / resolved / all tickets, newest first."""
    status = (status or "open").lower()
    try:
        limit = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit = 10
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if status == "all":
            cur.execute(
                "SELECT ticket_id, summary, priority, category, status, reporter_name, created_at "
                "FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            cur.execute(
                "SELECT ticket_id, summary, priority, category, status, reporter_name, created_at "
                "FROM incidents WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
        rows = cur.fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM incidents" + ("" if status == "all" else " WHERE status = ?"),
            () if status == "all" else (status,),
        ).fetchone()[0]
    finally:
        conn.close()
    return json.dumps({
        "status_filter": status,
        "total_matching": total,
        "tickets": [
            {"ticket_id": r[0], "summary": r[1], "priority": r[2], "category": r[3],
             "status": r[4], "reporter": r[5], "created_at": r[6]}
            for r in rows
        ],
    })


def get_incident(ticket_id: str) -> str:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT ticket_id, summary, priority, category, status, reporter_name, created_at, "
            "escalation_reason, escalated_at FROM incidents WHERE ticket_id = ?",
            (ticket_id.upper().strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return json.dumps({"error": f"Ticket {ticket_id} not found."})
    keys = ("ticket_id", "summary", "priority", "category", "status", "reporter",
            "created_at", "escalation_reason", "escalated_at")
    return json.dumps(dict(zip(keys, row)))


def escalate_ticket(ticket_id: str, reason: str) -> str:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM incidents WHERE ticket_id = ?", (ticket_id.upper(),))
        row = cur.fetchone()
        if not row:
            return json.dumps({"error": f"Ticket {ticket_id} not found."})
        if row[1] == "escalated":
            return json.dumps({"error": f"Ticket {ticket_id} is already escalated."})
        cur.execute(
            "UPDATE incidents SET status = 'escalated', escalation_reason = ?, escalated_at = ? WHERE ticket_id = ?",
            (reason, datetime.now(timezone.utc).isoformat(), ticket_id.upper()),
        )
        conn.commit()
        return json.dumps(
            {
                "ticket_id": ticket_id.upper(),
                "status": "Escalated",
                "reason": reason,
                "message": f"Ticket {ticket_id.upper()} has been escalated.",
            }
        )
    finally:
        conn.close()
