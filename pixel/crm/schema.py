SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY, session_id INTEGER NOT NULL, status TEXT NOT NULL, total TEXT NOT NULL,
    currency TEXT NOT NULL, fallback INTEGER NOT NULL, llm_cost_usd TEXT NOT NULL, created_at TEXT NOT NULL,
    sent_at TEXT, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_session ON quotes (session_id, created_at);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, session_id INTEGER NOT NULL, team_reference TEXT NOT NULL,
    booking_reference TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_reference ON bookings (booking_reference);
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY, session_id INTEGER NOT NULL, booking_id TEXT NOT NULL, customer TEXT NOT NULL,
    origin TEXT NOT NULL, destination TEXT NOT NULL, booking_reference TEXT, status TEXT NOT NULL,
    updated_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_lane ON shipments (customer, origin, destination);
"""
