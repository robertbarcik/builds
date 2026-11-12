"""Runtime schema management for the new ops tables.

Existing tables (employees, assets, incidents) stay owned by scripts/seed_db.py.
These CREATE IF NOT EXISTS statements run at startup so existing databases
keep working without re-seeding.
"""

import sqlite3

from app.config import DATABASE_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT,
    message TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS request_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL,
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON request_metrics(ts);
"""


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DATABASE_PATH, timeout=5)


def ensure_schema() -> None:
    conn = get_conn()
    try:
        # WAL: the simulation writes concurrently with the chat pipeline
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
