"""SQLite. Each package owns its tables in a `SCHEMA` string; `init` runs them all."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, direction TEXT NOT NULL, kind TEXT, purpose TEXT,
    sender TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    in_reply_to INTEGER, sent_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, customer TEXT NOT NULL, state TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'auto',
    received_at TEXT NOT NULL, deadline_at TEXT, quote_message_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session_id INTEGER, kind TEXT NOT NULL,
    actor TEXT NOT NULL, message_id INTEGER, payload TEXT NOT NULL, tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd TEXT NOT NULL DEFAULT '0', duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    from pixel.crm.schema import SCHEMA as crm
    from pixel.pricing.store import SCHEMA as pricing

    for schema in (SCHEMA, crm, pricing):
        conn.executescript(schema)


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value)
    )
