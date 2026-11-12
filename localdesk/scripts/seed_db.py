#!/usr/bin/env python3
"""Seed the SQLite database with synthetic employee and asset data."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DATABASE_PATH


def seed():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()

    # Create tables
    cur.executescript(
        """
        DROP TABLE IF EXISTS employees;
        DROP TABLE IF EXISTS assets;
        DROP TABLE IF EXISTS incidents;

        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            email TEXT NOT NULL,
            customer_tier TEXT NOT NULL DEFAULT 'bronze'
        );

        CREATE TABLE assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_tag TEXT UNIQUE NOT NULL,
            employee_id TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            model TEXT NOT NULL,
            assigned_date TEXT NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
        );

        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT UNIQUE,
            summary TEXT NOT NULL,
            priority TEXT NOT NULL,
            category TEXT NOT NULL,
            reporter_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            escalation_reason TEXT,
            escalated_at TEXT,
            created_at TEXT NOT NULL
        );
    """
    )

    # Seed employees
    employees = [
        ("EMP-001", "Anna Kováčová", "Engineering", "anna.kovacova@localdesk-services.eu", "gold"),
        ("EMP-002", "Martin Horváth", "Engineering", "martin.horvath@localdesk-services.eu", "gold"),
        ("EMP-003", "Jana Novotná", "Finance", "jana.novotna@localdesk-services.eu", "silver"),
        ("EMP-004", "Peter Szabó", "Finance", "peter.szabo@localdesk-services.eu", "silver"),
        ("EMP-005", "Eva Tóthová", "Marketing", "eva.tothova@localdesk-services.eu", "bronze"),
        ("EMP-006", "Tomáš Molnár", "Marketing", "tomas.molnar@localdesk-services.eu", "bronze"),
        ("EMP-007", "Lucia Nagyová", "HR", "lucia.nagyova@localdesk-services.eu", "silver"),
        ("EMP-008", "Michal Varga", "IT Operations", "michal.varga@localdesk-services.eu", "gold"),
        ("EMP-009", "Katarína Balázsová", "Sales", "katarina.balazsova@localdesk-services.eu", "silver"),
        ("EMP-010", "Juraj Fekete", "Sales", "juraj.fekete@localdesk-services.eu", "silver"),
        ("EMP-011", "Zuzana Bíróová", "Legal", "zuzana.biroova@localdesk-services.eu", "gold"),
        ("EMP-012", "Ondrej Kiss", "Engineering", "ondrej.kiss@localdesk-services.eu", "gold"),
        ("EMP-013", "Mária Lakatos", "Support", "maria.lakatos@localdesk-services.eu", "silver"),
        ("EMP-014", "Štefan Papp", "Support", "stefan.papp@localdesk-services.eu", "silver"),
        ("EMP-015", "Barbora Juhász", "Design", "barbora.juhasz@localdesk-services.eu", "bronze"),
        ("EMP-016", "Dávid Farkas", "Engineering", "david.farkas@localdesk-services.eu", "gold"),
        ("EMP-017", "Ivana Oláh", "Finance", "ivana.olah@localdesk-services.eu", "silver"),
        ("EMP-018", "Róbert Mészáros", "IT Operations", "robert.meszaros@localdesk-services.eu", "gold"),
        ("EMP-019", "Simona Gál", "HR", "simona.gal@localdesk-services.eu", "silver"),
        ("EMP-020", "Adrián Takács", "Engineering", "adrian.takacs@localdesk-services.eu", "gold"),
    ]
    cur.executemany(
        "INSERT INTO employees (employee_id, name, department, email, customer_tier) VALUES (?, ?, ?, ?, ?)",
        employees,
    )

    # Seed assets
    assets = [
        ("LT2024-0101", "EMP-001", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-03-15"),
        ("MN2024-0101", "EMP-001", "monitor", "Dell U2723QE 27\" 4K", "2024-03-15"),
        ("LT2024-0102", "EMP-002", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-04-01"),
        ("LT2023-0201", "EMP-003", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-09-10"),
        ("LT2023-0202", "EMP-004", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-09-10"),
        ("LT2024-0301", "EMP-005", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2024-01-20"),
        ("LT2024-0302", "EMP-006", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2024-01-20"),
        ("LT2023-0401", "EMP-007", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-11-05"),
        ("LT2024-0501", "EMP-008", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-02-28"),
        ("MN2024-0501", "EMP-008", "monitor", "Dell U2723QE 27\" 4K", "2024-02-28"),
        ("DK2024-0501", "EMP-008", "dock", "Lenovo ThinkPad USB-C Dock Gen 2", "2024-02-28"),
        ("LT2023-0601", "EMP-009", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-07-15"),
        ("LT2023-0602", "EMP-010", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-08-01"),
        ("LT2024-0701", "EMP-011", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-05-10"),
        ("LT2024-0801", "EMP-012", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-06-01"),
        ("MN2024-0801", "EMP-012", "monitor", "LG 27UP850-W 27\" 4K", "2024-06-01"),
        ("LT2023-0901", "EMP-013", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-10-15"),
        ("LT2023-0902", "EMP-014", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-10-15"),
        ("MB2024-1001", "EMP-015", "laptop", "Apple MacBook Pro 14\" M3 Pro (18GB)", "2024-04-20"),
        ("LT2024-1101", "EMP-016", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-07-01"),
        ("MN2024-1101", "EMP-016", "monitor", "Dell U2723QE 27\" 4K", "2024-07-01"),
        ("LT2023-1201", "EMP-017", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-12-01"),
        ("LT2024-1301", "EMP-018", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-01-15"),
        ("SV2024-1301", "EMP-018", "server_access", "Admin access to SRV-PROD-01, SRV-PROD-02", "2024-01-15"),
        ("LT2023-1401", "EMP-019", "laptop", "Lenovo ThinkPad L14 Gen 5 (i5/16GB)", "2023-06-20"),
        ("LT2024-1501", "EMP-020", "laptop", "Lenovo ThinkPad T14s Gen 5 (i7/32GB)", "2024-08-01"),
        ("MN2024-1501", "EMP-020", "monitor", "Dell U2723QE 27\" 4K", "2024-08-01"),
        ("PH2024-0101", "EMP-001", "phone", "iPhone 15 Pro (company-managed)", "2024-03-15"),
        ("KB2024-0801", "EMP-012", "keyboard", "Logitech MX Keys S", "2024-06-01"),
        ("MS2024-0801", "EMP-012", "mouse", "Logitech MX Master 3S", "2024-06-01"),
    ]
    cur.executemany(
        "INSERT INTO assets (asset_tag, employee_id, asset_type, model, assigned_date) VALUES (?, ?, ?, ?, ?)",
        assets,
    )

    # Seed incidents (so dashboard has data on first load).
    # Timestamps are relative to seed time so the SLA radar starts with a
    # realistic mix: some tickets comfortably inside their SLA, one breached.
    def ago(hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    incidents = [
        ("INC-00001", "Email client not syncing after Outlook update", "medium", "software",
         "Anna Kováčová", "open", None, None, ago(3)),
        ("INC-00002", "VPN disconnects every 30 minutes on WiFi", "high", "network",
         "Martin Horváth", "open", None, None, ago(5)),
        ("INC-00003", "Cannot access shared drive S:\\Finance after password reset", "high", "access",
         "Jana Novotná", "escalated", "User locked out of critical finance data for 24+ hours",
         ago(20), ago(26)),
        ("INC-00004", "Printer on 3rd floor not responding to any jobs", "low", "hardware",
         "Eva Tóthová", "open", None, None, ago(8)),
        ("INC-00005", "Laptop screen flickering intermittently", "medium", "hardware",
         "Ondrej Kiss", "open", None, None, ago(14)),
        ("INC-00006", "MFA push notifications not arriving on new phone", "high", "access",
         "Lucia Nagyová", "resolved", None, None, ago(30)),
        ("INC-00007", "Network outage in Building B affecting 40+ users", "critical", "network",
         "Michal Varga", "escalated", "Major outage impacting full building — requires infrastructure team",
         ago(6), ago(7)),
        ("INC-00008", "New employee needs full IT onboarding — laptop, accounts, VPN", "low", "other",
         "Simona Gál", "open", None, None, ago(2)),
        ("INC-00009", "SAP client crashing on launch after Windows update", "critical", "software",
         "Dávid Farkas", "open", None, None, ago(1)),
        ("INC-00010", "Remote desktop session freezing during video calls", "medium", "software",
         "Katarína Balázsová", "resolved", None, None, ago(40)),
    ]
    cur.executemany(
        "INSERT INTO incidents (ticket_id, summary, priority, category, reporter_name, status, "
        "escalation_reason, escalated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        incidents,
    )

    # Ops tables (also created at runtime by app.db.ensure_schema — kept in
    # sync here so a fresh seed matches; never dropped, data survives re-seeds)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, event_type TEXT NOT NULL, severity TEXT NOT NULL,
            source TEXT, message TEXT NOT NULL, metadata_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS request_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, session_id TEXT, role TEXT NOT NULL, model TEXT NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            cost_usd REAL, duration_s REAL);
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(ts);
    """)

    conn.commit()
    conn.close()
    print(f"Database seeded at {DATABASE_PATH}")
    print(f"  - {len(employees)} employees")
    print(f"  - {len(assets)} assets")
    print(f"  - {len(incidents)} incidents")


if __name__ == "__main__":
    seed()
