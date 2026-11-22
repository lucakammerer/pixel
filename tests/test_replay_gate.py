import asyncio
import json
from pathlib import Path

from pixel.agents.rfq import extract
from pixel.core import db
from pixel.pricing import selection
from tools import replay_gate

ROUND = json.loads((Path(__file__).parent / "fixtures" / "replay-gate.json").read_text())


def _live(path: Path) -> str:
    """Round 155 as the live database held it: door-to-port, read by rules, four truckers, the balanced one quoted.
    An airline price that came after the quote and beats it is stored too."""
    mail, quote = ROUND["mail"], ROUND["quote"]
    sid, late = mail["id"], ROUND["options"][0] | {"freight": "1.00", "total": "1.00", "total_eur": "1.00"}
    conn = db.connect(str(path))
    db.init(conn)
    conn.execute("INSERT INTO pickup_zones VALUES (?, ?, ?)", ROUND["zone"])
    conn.execute("INSERT INTO messages VALUES (?, ?, 'in', 'customer_rfq', NULL, ?, ?, ?, ?, NULL, ?, ?)",
                 (sid, sid, mail["from"], mail["to"], mail["subject"], mail["body"], mail["received_at"],
                  json.dumps({"raw": mail})))  # fmt: skip
    conn.execute("INSERT INTO quotes VALUES (?, ?, 'sent', ?, 'EUR', 0, '0', 't', 't', ?)",
                 (quote["id"], sid, quote["total"], json.dumps(quote)))  # fmt: skip
    for kind, session_id, message_id, payload in (
        ("rfq.extracted", sid, sid, ROUND["rfq"]),
        *(("option.found", sid, None, {"option": o}) for o in ROUND["options"]),
        ("quote.drafted", sid, None, {"quote_id": quote["id"], "option": quote["option"]}),
        ("quote.sent", sid, None, {"quote": quote["id"]}),
        ("option.found", sid, None, {"option": late}),
        ("score", None, None, {"round": ROUND["round"], "verdict": "accepted"}),
        ("feedback", sid, None, {"round": ROUND["round"], "verdict": "accepted"}),
    ):
        conn.execute("INSERT INTO events (ts, session_id, kind, actor, message_id, payload) VALUES ('t', ?, ?, 'x', ?, ?)",
                     (session_id, kind, message_id, json.dumps(payload)))  # fmt: skip
    conn.close()
    return str(path)


def test_a_round_read_and_priced_as_quoted_passes_and_its_database_is_not_written(tmp_path, capsys):
    live = _live(tmp_path / "live.sqlite3")
    before = Path(live).read_bytes()
    assert asyncio.run(replay_gate.gate(live)) == 0, capsys.readouterr().out
    assert Path(live).read_bytes() == before


def test_another_trucker_or_reading_fails_with_round_field_and_both_values(tmp_path, capsys, monkeypatch):
    live = _live(tmp_path / "live.sqlite3")
    with monkeypatch.context() as broken:  # the cheapest trucker instead of the balanced one
        broken.setattr(selection, "balanced", lambda bids, *_: min(bids, key=lambda b: b.total_eur))
        assert asyncio.run(replay_gate.gate(live)) == 1
    assert "session 46441 differs: trucker: quoted Westfalenroute, today Spessart Spedition" in capsys.readouterr().out
    monkeypatch.setattr(extract, "_pickup", lambda text: (True, None))  # the pickup postcode dropped, as #334 did
    assert asyncio.run(replay_gate.gate(live)) == 1
    assert "session 46441 differs: pickup_postcode: read 42763, today None" in capsys.readouterr().out
