import json
import sqlite3

from pixel.core import db
from tools import card_proof


def test_a_rate_is_measured_against_the_card_as_it_stood_before_it(tmp_path):
    """A card that already holds the rate itself, or a later read of its bracket, proves nothing."""
    source = str(tmp_path / "live.sqlite3")
    live = db.connect(source)
    db.init(live)
    db.put(live, "rate_card.breaks.A1", '["0", "1000"]')

    def read(kg: str, total: str) -> None:
        live.execute("INSERT INTO events (ts, kind, actor, payload) VALUES ('2026-11-22T05:00:00+00:00', ?, 'pricing', ?)",
                     ("rate_card.read", json.dumps({"origin": "AAA", "destination": "BBB", "code": "A1",
                                                    "carrier": "Air One", "kg": kg, "total": total, "currency": "EUR",
                                                    "fx_eur": None, "transit_days": 2, "version": "v1"})))  # fmt: skip

    read("1000", "3000.00")
    option = {"source": "portal", "carrier": "Air One", "carrier_code": "A1", "origin": "AAA", "destination": "BBB",
              "chargeable_kg": "1200", "billed_kg": "1200", "total": "3636.00", "currency": "EUR", "fx_eur": None,
              "transit_days": 2, "rate_id": "R-1", "trucker": None, "dataset_version": "v1"}  # fmt: skip
    live.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES"
                 " ('2026-11-22T05:06:00+00:00', 7, 'option.found', 'pricing', ?)", (json.dumps({"option": option}),))  # fmt: skip
    read("1000", "3030.00")
    live.close()

    result = card_proof.measure(source)

    assert (result["rates"], result["priced"], result["within_pct"]) == (1, 1, {"0.1": 0, "1": 1, "5": 1})
    assert [(c["card"], c["off_pct"], c["cause"]) for c in result["worst"]] == [("3600.00", -0.99, "same")]
    assert sqlite3.connect(source).execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
