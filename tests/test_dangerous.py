import json
from datetime import UTC, datetime

from pixel.agents.rfq import dangerous
from pixel.agents.rfq.extract import DG_DOCUMENTS
from pixel.config import settings
from pixel.contracts import Rfq
from pixel.core.context import Context

BODY = "Hello,\n\nwe can offer 2187.10 EUR all-in.\n\nJust reply to book.\n\nBest regards\nLena"
NOW = datetime(2026, 11, 21, 16, tzinfo=UTC)


def rfq(*missing):
    return Rfq(session_id=7, message_id=7, customer="c@x", origin="HAM", destination="YYZ", weight_kg=855,
               received_at=NOW, deadline_at=NOW, missing=missing)  # fmt: skip


def test_the_quote_takes_the_declared_goods_from_the_marked_request(conn):
    ctx = Context(settings(), conn, None, None, None, None)
    conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, 7, 'rfq.dangerous_goods', 'rfq', ?)",
                 (NOW.isoformat(), json.dumps({"declared": "UN1950, Class 2.1, Aerosols, flammable", "source": "x.pdf"})))  # fmt: skip
    assert dangerous.declared(ctx, rfq(DG_DOCUMENTS)) == "UN1950, Class 2.1, Aerosols, flammable"
    assert dangerous.declared(ctx, rfq()) is None  # ordinary goods: no note, whatever events exist


def test_the_note_names_the_goods_and_asks_for_the_papers_before_the_closing():
    body = dangerous.with_note(BODY, "UN1950, Class 2.1, Aerosols, flammable", "en")
    paragraphs = body.split("\n\n")
    assert paragraphs[1] == "we can offer 2187.10 EUR all-in." and paragraphs[3:] == BODY.split("\n\n")[2:]
    assert "UN1950, Class 2.1" in paragraphs[2] and "DGD" in paragraphs[2] and "MSDS" in paragraphs[2]
    assert "Gefahrgut" in dangerous.with_note(BODY, "UN1133", "de") and "DGD" in dangerous.with_note(
        BODY, "UN1133", "de"
    )
