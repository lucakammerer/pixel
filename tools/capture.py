"""A stored session as a replay fixture and a pytest, with one command: a lost round becomes a failing test.

uv run python -m tools.capture <database> <session>                 # writes the fixture, prints the test
uv run python -m tools.capture <database> <session> --name <slug>   # tests/fixtures/sessions/<slug>.json

`tests/fixtures/sessions/<name>.json` holds the session's incoming mails as the API returned them (the request, the
carriers', truckers' and rate desk's answers, later customer mails), in the list shape `tools/replay.py` replays.
`<name>.state.json` holds the rows the session met on arrival: tariffs ingested by then, carriers, pickup zones, the
rate card, our own mails of the session (an answer finds its session through the bid request it answers), the
customer's earlier requests and the events read across sessions. `tools.replay tests/fixtures/sessions/<name>.json`
replays both, and answers each model call the session made live with what the model returned then (`llm.call`
`answer`, recorded since 22.11.). Attachment bytes are not stored in the database: a file the session had is named as a to-do.

The printed test asserts what the scorer wanted where the database holds it (an accepted round: lane, airline,
trucker, the best price within 5 %, language) and what its verdict implies where it does not (too high: below our
total / 1.05; excluded airline: another airline; wrong language: another language), each unknown as `TODO(capture)`.
The capture replays the fixture at once and says whether it reproduces the live quote: a test on a fixture that does
not reproduce it proves nothing. No network, no key, nothing from `.env`; the database is opened read-only.
"""

import argparse
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pixel.contracts import Event
from pixel.pricing import pickup
from tools import replay

ROOT = Path(__file__).parent.parent
SESSIONS = ROOT / "tests" / "fixtures" / "sessions"
BAND = Decimal("0.05")  # the scorer accepts a quote within 5 % of its best price (R16)
# Events one session reads of earlier ones besides those `state` picks: stated sheet fx, airlines' availability,
# lasting exclusions, the scorer's truckers (`feedback.pickup_rule`).
SHARED = ("tariff.ingested", "carrier.availability", "customer.excludes", "feedback")
EVENT = ("id", "ts", "session_id", "kind", "actor", "message_id", "payload")
PICKUP = (
    "source",
    "carrier",
    "origin",
    "destination",
    "chargeable_kg",
    "billed_kg",
    "freight",
    "pickup",
    "trucker",
    "pickup_hours",
    "total",
    "currency",
    "total_eur",
    "transit_days",
)  # what `feedback.pickup_rule` and `pickup._answers` read
HISTORY = ("rfq.extracted", "quote.prose", "quote.sent")  # of the customer's earlier sessions


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def state(conn: sqlite3.Connection, session_id: int) -> dict:
    """The rows of `conn` the session could read when its request arrived, as far as it reads them."""
    arrived = conn.execute("SELECT received_at, customer FROM sessions WHERE id = ?", (session_id,)).fetchone()
    first = conn.execute("SELECT MIN(id) FROM events WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
    earlier = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM sessions WHERE LOWER(customer) = LOWER(?) AND id < ?", (arrived["customer"], session_id)
        )
    ]
    lanes = {
        (p["origin"], p["destination"])
        for p in _payloads(conn, "session_id = ? AND kind = 'rfq.extracted'", (session_id,))
    }
    events = _rows(
        conn, f"SELECT * FROM events WHERE id < ? AND kind IN ({','.join('?' * len(SHARED))})", (first, *SHARED)
    )
    events += _rows(
        conn,
        f"SELECT * FROM events WHERE session_id IN ({','.join('?' * len(earlier))}) AND kind IN"
        f" ({','.join('?' * len(HISTORY))}) AND id < ?",
        (*earlier, *HISTORY, first),
    )
    trucked = {e["session_id"] for e in events if e["kind"] == "feedback" and '"trucker"' in e["payload"]}
    since = (datetime.fromisoformat(arrived["received_at"]) - timedelta(hours=pickup.MEMORY_H)).isoformat()
    remembered: set[int] = set()
    newest: dict[tuple, dict] = {}
    for e in _rows(
        conn,
        "SELECT * FROM events WHERE id < ? AND kind IN ('option.found', 'rate_card.read', 'carrier.lanes') ORDER BY id",
        (first,),
    ):
        p = json.loads(e["payload"])
        o = p.get("option") or p
        if e["kind"] == "carrier.lanes":  # `bids.learned_lanes` reads each airline's newest list
            newest[("lanes", p["carrier"].casefold())] = e
        elif (o.get("origin"), o.get("destination")) in lanes and (
            e["kind"] == "rate_card.read" or o["source"] == "portal"
        ):
            events.append(e)  # the rate card of the lane
        elif e["kind"] == "option.found" and o.get("fx_eur") is not None:
            newest[("fx", o["source"], o["currency"])] = e  # `engine._fx_eur`, `bids.known_fx`: the newest rate
        elif e["kind"] == "option.found" and o.get("trucker") and (e["ts"] >= since or e["session_id"] in trucked):
            # `pickup._answers`, the price memory of a zone's truckers, and `feedback.pickup_rule`
            events.append(e | {"payload": json.dumps({"option": {k: o.get(k) for k in PICKUP}})})
            remembered.add(e["session_id"])
    events += newest.values()
    events += _rows(conn, "SELECT * FROM events WHERE id < ? AND (kind = 'option.vetoed' AND ts >= ? OR kind ="
                    f" 'rfq.extracted' AND session_id IN ({','.join('?' * len(remembered))}))",
                    (first, since, *remembered))  # fmt: skip
    customers = "SELECT * FROM messages WHERE id IN ({})".format(",".join("?" * len(earlier)))
    firsts = _rows(
        conn,
        "SELECT * FROM messages WHERE id IN (SELECT MIN(id) FROM messages WHERE kind = 'customer_rfq'"
        " AND id < ? GROUP BY sender)",
        (session_id,),
    )
    return {
        "tariffs": _rows(conn, "SELECT * FROM tariffs WHERE ingested_at <= ?", (arrived["received_at"],)),
        "carriers": _rows(conn, "SELECT * FROM carriers"),
        "pickup_zones": _rows(conn, "SELECT * FROM pickup_zones"),
        "kv": _rows(conn, "SELECT * FROM kv WHERE key LIKE 'rate_card.%'"),
        "sessions": _rows(
            conn, "SELECT * FROM sessions WHERE id IN ({})".format(",".join("?" * len(earlier))), tuple(earlier)
        ),
        # our mails of the session; without their text, the customer's earlier requests (`extract` finds one by its
        # subject) and each sender's first request (a lookalike address is told from the original by it)
        "messages": sorted(
            {
                m["id"]: m
                for m in [m | {"body": "", "data": "{}"} for m in firsts + _rows(conn, customers, tuple(earlier))]
                + _rows(conn, "SELECT * FROM messages WHERE session_id = ? AND direction = 'out'", (session_id,))
            }.values(),
            key=lambda m: m["id"],
        ),
        "events": sorted(
            ({k: v for k, v in e.items() if k in EVENT} for e in {e["id"]: e for e in events}.values()),
            key=lambda e: e["id"],
        ),
        "portal": portal(conn, session_id),
        "answers": replay.answers(conn, session_id),  # the model's live readings, replayed for free
    }


def _payloads(conn: sqlite3.Connection, where: str, args: tuple) -> list[dict]:
    return [json.loads(p) for (p,) in conn.execute(f"SELECT payload FROM events WHERE {where} ORDER BY id", args)]


def portal(conn: sqlite3.Connection, session_id: int) -> dict:
    """What the portal's API gave the session, served again by the replay (`replay._recorded`): its rates and the
    weight breaks of their airlines as the rate card holds them."""
    rates = [
        o
        for p in _payloads(conn, "session_id = ? AND kind = 'option.found'", (session_id,))
        if (o := p["option"])["source"] == "portal" and o.get("rate_id")
    ]
    codes = {o["carrier_code"] for o in rates}
    brackets = {
        code: json.loads(v)
        for k, v in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'rate_card.breaks.%'")
        if (code := k.removeprefix("rate_card.breaks.")) in codes
    }
    return {"rates": rates, "brackets": brackets}


def mails(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT data FROM messages WHERE session_id = ? AND direction = 'in' ORDER BY id", (session_id,)
    )
    return [json.loads(data)["raw"] for (data,) in rows]


def verdict(conn: sqlite3.Connection, session_id: int) -> dict:
    """The scorer's verdict on the session's quote (`feedback`), {} when it was not scored."""
    row = conn.execute(
        "SELECT payload FROM events WHERE kind = 'feedback' AND session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def quoted(events: list[Event], nth: int = 0) -> dict | None:
    """The first quote that left (`nth`: a later one, the answer to a changed request): lane, totals, airline,
    trucker and the language it was read in. None: no such quote."""
    sent = next(iter([e for e in events if e.kind == "quote.sent"][nth:]), None)
    if sent is None:
        return None
    here = [e for e in events if e.session_id == sent.session_id and e.id < sent.id]
    draft = next(
        e.payload
        for e in reversed(here)
        if e.kind == "quote.drafted" and e.payload.get("quote_id") == sent.payload["quote"]
    )
    read = next((e.payload for e in reversed(here) if e.kind == "rfq.extracted"), {})
    option = draft["option"]
    return {
        "lane": (option["origin"], option["destination"]),
        "total": Decimal(str(sent.payload["total"])),
        "currency": sent.payload["currency"],
        "total_eur": Decimal(str(option["total_eur"])),
        "airline": option["carrier"],
        "trucker": option.get("trucker"),
        "language": read.get("language"),
    }


def live(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """`quoted` on the session's stored events: what the live service sent."""
    rows = conn.execute("SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return quoted([Event.model_validate({**dict(r), "payload": json.loads(r["payload"])}) for r in rows])


def _py(value) -> str:
    """A literal as ruff formats it: double quotes."""
    if isinstance(value, tuple):
        return "(" + ", ".join(map(_py, value)) + ")"
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else repr(value)


def test_source(name: str, session_id: int, scored: dict, then: dict | None, todos: list[str]) -> str:
    """The pytest: what the scorer wanted, as far as the database and its verdict tell."""
    reason = scored.get("reason") or scored.get("verdict") or "not scored"
    accepted = scored.get("verdict") == "accepted"
    checks = ['assert quote is not None, "no quote left"']
    if then is None:
        todos.append("we sent no quote live: assert the lane, total and airline the request asks for")
    else:
        eur, best = then["total_eur"], scored.get("best")
        confirm = "" if accepted else "  # TODO(capture): our reading; confirm it against the mail"
        checks.append(f'assert quote["lane"] == {_py(then["lane"])}{confirm}')
        if accepted and best is not None:
            checks.append(
                f'assert abs(quote["total_eur"] - Decimal("{best}")) <= Decimal("{best}") * Decimal("{BAND}")'
            )
            checks.append(
                f'assert (quote["airline"], quote["trucker"]) == {_py((scored.get("carrier"), scored.get("trucker")))}'
            )
        elif reason == "too_high":
            checks.append(
                f'assert quote["total_eur"] < Decimal("{eur}") / Decimal("{1 + BAND}")  # TODO(capture): exact'
            )
        elif reason == "too_low":
            checks.append(
                f'assert quote["total_eur"] > Decimal("{eur}") / Decimal("{1 - BAND}")  # TODO(capture): exact'
            )
        elif reason == "excluded_airline":
            checks.append(f'assert quote["airline"] != {_py(then["airline"])}  # TODO(capture): the airline it allows')
        elif not accepted:
            todos.append(f"the scorer's reason {reason}: assert what it wanted")
        wrong = "!=  # TODO(capture): the request's language" if reason == "wrong_language" else "=="
        checks.append(f'assert quote["language"] {wrong.split("  ")[0]} {_py(then["language"])}' + wrong[2:])
    checks += [f'pytest.fail("TODO(capture): {t}")' for t in todos]
    body = "\n    ".join(checks)
    title = re.sub(r"\W+", "_", name).strip("_").lower()
    imports = "import asyncio\n" + ("from decimal import Decimal\n" if "Decimal(" in body else "")
    imports += "from pathlib import Path\n" + ("\nimport pytest\n" if "pytest." in body else "")
    return f'''{imports}
from tools import capture, replay


def test_{title}():
    """Session {session_id}, round {scored.get("round", "?")}: {reason}. Captured with tools.capture."""
    path = Path(__file__).parent / "fixtures" / "sessions" / "{name}.json"
    quote = capture.quoted(asyncio.run(replay.fixture(path)))
    {body}
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db")
    parser.add_argument("session", type=int)
    parser.add_argument("--name", help="the fixture's name; default round-<n>-<reason>")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    raws = mails(conn, args.session)
    if not raws:
        parser.error(f"no incoming mail of session {args.session}")
    scored, then = verdict(conn, args.session), live(conn, args.session)
    name = (
        args.name
        or f"round-{scored.get('round', args.session)}-{scored.get('reason') or scored.get('verdict') or 'unscored'}"
    )
    SESSIONS.mkdir(exist_ok=True)
    path = SESSIONS / f"{name}.json"
    path.write_text(json.dumps(raws, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.with_suffix(".state.json").write_text(
        json.dumps(state(conn, args.session), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    todos = [
        f"attachment {a.get('filename')!r} of mail {raw['id']} is not stored: add its content_base64"
        for raw in raws
        for a in raw.get("attachments") or ()
        if not a.get("content_base64")
    ]
    models = {
        p["purpose"]
        for (raw,) in conn.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'llm.call'", (args.session,)
        )
        if "answer" not in (p := json.loads(raw))
    }
    now = quoted(asyncio.run(replay.fixture(path)))
    print(f"# wrote {path.relative_to(ROOT)} and its .state.json")
    print(f"# live:   {then}")
    print(f"# replay: {now}")
    if then != now:
        print(
            "# the replay does not reproduce the live quote; the test below checks today's code. Live model calls"
            f" without a recorded answer, which fail offline: {', '.join(sorted(models)) or 'none'}"
        )
    print(test_source(name, args.session, scored, then, todos), end="")


if __name__ == "__main__":
    main()
