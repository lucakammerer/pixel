import sqlite3
from contextlib import closing

from tools.supply_census import _scored_rounds


def test_score_uses_exact_receipt_and_selects_only_the_quote_the_board_scored():
    receipt = "2025-01-15T09:30:00+00:00"
    ambiguous = "2025-01-15T09:36:00+00:00"
    quotes = {1: [
        {"quote_id": "first", "sent_at": "2025-01-15T09:30:30+00:00"},
        {"quote_id": "second", "sent_at": "2025-01-15T10:31:00+01:00"},
    ]}  # fmt: skip
    scores = {
        1: {"sent_at": receipt, "answered_at": "2025-01-15T09:31:00Z"},
        2: {"sent_at": ambiguous},
        3: {"sent_at": "2025-01-15T09:30:01+00:00"},
        4: {"sent_at": ambiguous},
    }
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE sessions (id INTEGER, received_at TEXT)")
        db.executemany("INSERT INTO sessions VALUES (?, ?)", [(1, receipt), (2, ambiguous), (3, ambiguous)])
        first, duplicate, near, explicit = _scored_rounds(db, scores, {4: 2}, quotes)

    assert (first["session_id"], first["session_link"], first["quote_ids"], first["scored_quote_id"]) == (
        1, "exact_receipt", ["first", "second"], "second"
    )  # fmt: skip
    assert duplicate["session_id"] is None
    assert near["session_id"] is None
    assert (explicit["session_id"], explicit["session_link"], explicit["scored_quote_id"]) == (2, "feedback", None)
