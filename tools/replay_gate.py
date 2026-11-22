"""The deploy gate: this checkout must read and price the last requests the scorer accepted as the live service did.

uv run python -m tools.replay_gate                          # the last 20 accepted requests of the local database
uv run python -m tools.replay_gate <database> --last 50     # of that database; deploy/cd.sh runs it on the live one

Per accepted request (`score` verdict accepted; its `feedback` names the session), without network, model or key:

- Pricing: the options the session had when its quote was drafted (`option.found`, without those vetoed; for a
  quote that answers a changed request those found for the change, as `agent.requote`), through today's
  `engine.combine` and `selection.select` at the request as it was priced. Total in EUR, airline and trucker must be
  the sent quote's.
- Reading: the request mail through today's `extract.read_request`, against the `rfq.extracted` it left then, on
  FIELDS; weight and volume at two decimals, as the scorer reads them (round 136). Not replayed where a model read
  or cross-checked the request, we asked the customer or the mail has attachments (their bytes are not stored): said
  in the output.

One line with `differs:` per difference, exit 1 on any. It works on a copy made with the sqlite backup API and never
writes to the database it is given.
"""

import argparse
import asyncio
import dataclasses
import json
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from pixel.agents.rfq import extract
from pixel.api.mailbox import to_message
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Quote, Rfq
from pixel.core import feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import calc, engine, selection, session
from tools import replay

FIELDS = ("origin", "destination", "weight_kg", "volume_cbm", "pieces", "preference", "door_pickup",
          "pickup_postcode", "pickup_zone", "excluded_carriers", "required_carriers", "ready_on", "arrive_by",
          "max_transit_days")  # fmt: skip
MODEL_READ = ("rfq.extract", "rfq.crosscheck")


def accepted(conn: sqlite3.Connection, last: int) -> list[tuple[int, int]]:
    """(round, session) of the newest requests whose latest verdict is accepted, newest first."""
    verdicts, sessions = {}, {}
    for kind, session_id, payload in conn.execute(
        "SELECT kind, session_id, payload FROM events WHERE kind IN ('score', 'feedback') ORDER BY id"
    ):
        data = json.loads(payload)
        if kind == "score":
            verdicts[data["round"]] = data["verdict"]
        elif session_id is not None:
            sessions[data["round"]] = session_id
    rounds = sorted((r for r in sessions if verdicts.get(r) == "accepted"), reverse=True)
    return [(r, sessions[r]) for r in rounds[:last]]


def _events(conn: sqlite3.Connection, session_id: int, kind: str, before: int | None = None) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, message_id, payload FROM events WHERE session_id = ? AND kind = ? AND id < COALESCE(?, id + 1)"
        " ORDER BY id",
        (session_id, kind, before),
    ).fetchall()


def _known(model: type, stored: dict) -> dict:
    """As `session.option`: a field a hand-deployed branch wrote and main lacks is dropped."""
    return {k: v for k, v in stored.items() if k in model.model_fields}


def priced(conn: sqlite3.Connection, session_id: int, rule: str) -> list[str]:
    """What today's pickup choice and selection quote from the session's options, against the quote it sent."""
    sent = _events(conn, session_id, "quote.sent")
    if not sent:
        return ["no quote.sent"]
    quote_id = json.loads(sent[-1]["payload"])["quote"]
    draft = next(d for d in reversed(_events(conn, session_id, "quote.drafted"))
                 if json.loads(d["payload"])["quote_id"] == quote_id)  # fmt: skip
    stored = json.loads(conn.execute("SELECT data FROM quotes WHERE id = ?", (quote_id,)).fetchone()["data"])
    quote = Quote.model_validate(_known(Quote, stored) | {"rfq": _known(Rfq, stored["rfq"]),
                                                          "option": session.option(stored["option"])})  # fmt: skip
    rfq, drafted = quote.rfq, json.loads(draft["payload"])
    out = [json.loads(v["payload"]) for v in _events(conn, session_id, "option.vetoed", draft["id"])]
    found = [(o["id"], session.option(json.loads(o["payload"])["option"]))
             for o in _events(conn, session_id, "option.found", draft["id"])]  # fmt: skip
    found = [(i, o) for i, o in found if not any(session.vetoed(o, v) for v in out)]
    # `agent.requote` marks a changed request with an `rfq.extracted` for the mail that changed it
    changed = [
        e["id"] for e in _events(conn, session_id, "rfq.extracted", draft["id"]) if e["message_id"] != session_id
    ]
    fresh = [o for i, o in found if changed and i > changed[-1]]
    options = engine.combine(rfq, fresh, rule) or engine.combine(rfq, [o for _, o in found], rule)
    if rfq.door_pickup:
        options = [o for o in options if o.trucker] or options
    if not options:
        return ["today no stored option prices the request"]
    required = drafted.get("required") or {}
    arrive_by = date.fromisoformat(required["arrive_by"]) if not rfq.arrive_by and required.get("arrive_by") else None
    pick = selection.select(rfq, options, only=required.get("only_airlines") or (), arrive_by=arrive_by,
                            ready=calc.ready_day(rfq) if arrive_by else None)  # fmt: skip
    insurance = Decimal(drafted.get("insurance_eur") or 0)
    then = (quote.option.total_eur - insurance, quote.option.carrier, quote.option.trucker)
    today = (pick.total_eur, pick.carrier, pick.trucker)
    return [f"{name}: quoted {a}, today {b}"
            for name, a, b in zip(("total_eur", "airline", "trucker"), then, today, strict=True) if a != b]  # fmt: skip


def unread(conn: sqlite3.Connection, session_id: int) -> str | None:
    """Why the session's reading cannot be replayed offline, None when it can."""
    purposes = {json.loads(e["payload"]).get("purpose") for e in _events(conn, session_id, "llm.call")}
    if model := sorted(purposes & set(MODEL_READ)):
        return f"a model call read it ({', '.join(model)})"
    if conn.execute("SELECT 1 FROM messages WHERE session_id = ? AND purpose = 'question'", (session_id,)).fetchone():
        return "we asked the customer"
    row = conn.execute("SELECT data FROM messages WHERE id = ?", (session_id,)).fetchone()
    if row and json.loads(row["data"])["raw"].get("attachments"):
        return "its attachments are not stored"
    return None


async def read(ctx: Context, session_id: int) -> list[str]:
    """Today's reading of the request mail against the reading stored then."""
    then = next((e for e in _events(ctx.db, session_id, "rfq.extracted") if e["message_id"] == session_id), None)
    row = ctx.db.execute("SELECT data FROM messages WHERE id = ? AND direction = 'in'", (session_id,)).fetchone()
    if not then or not row:
        return ["no stored request or rfq.extracted"]
    payload = json.loads(then["payload"])
    stored = Rfq.model_validate(_known(Rfq, payload))
    message = to_message(json.loads(row["data"])["raw"], "in")
    token = timing.clock.set(lambda: message.sent_at)
    try:
        today, _ = await extract.read_request(ctx, message, deadline_for(message.sent_at, ctx.settings))
    except Exception as e:  # noqa: BLE001  offline the model's call fails: the rules no longer read this request
        return [f"today the rules do not read it ({type(e).__name__})"]
    finally:
        timing.clock.reset(token)
    pairs = [(f, getattr(stored, f), getattr(today, f)) for f in FIELDS if f in payload]
    return [f"{f}: read {a}, today {b}" for f, a, b in pairs
            if (round(a, 2) != round(b, 2) if isinstance(a, Decimal) else a != b)]  # fmt: skip


async def gate(db: str, last: int = 20) -> int:
    with tempfile.TemporaryDirectory() as folder:
        config = dataclasses.replace(settings(), dry_run=True, api_key="", db_path=str(Path(folder) / "gate.sqlite3"))
        replay._copy(db, config.db_path, [])
        ctx = build_context(config)
        await ctx.mailbox.client.aclose()
        offline = httpx.AsyncClient(base_url=config.api_base, transport=httpx.MockTransport(replay._offline))
        ctx.mailbox.client = ctx.portal.client = ctx.gateway.client = ctx.board.client = offline
        rounds, rule, red = accepted(ctx.db, last), feedback.pickup_rule(ctx), 0
        for number, session_id in rounds:  # newest first: a reading left in the copy is never an earlier request's
            try:
                differs = priced(ctx.db, session_id, rule)
            except Exception as e:  # noqa: BLE001  a stored quote or option today's contracts cannot read
                differs = [f"pricing: {type(e).__name__}: {str(e).splitlines()[0]}"]
            skipped = unread(ctx.db, session_id)
            differs += [] if skipped else await read(ctx, session_id)
            red += bool(differs)
            for line in differs:
                print(f"round {number} session {session_id} differs: {line}")
            if skipped and not differs:
                print(f"round {number} session {session_id}: priced as quoted; reading not replayed, {skipped}")
        await offline.aclose()
        ctx.db.close()
    print(f"{len(rounds) - red} of {len(rounds)} accepted requests read and priced as quoted")
    return 1 if red or not rounds else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default=settings().db_path)
    parser.add_argument("--last", type=int, default=20, help="how many accepted requests")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(gate(args.db, args.last)))


if __name__ == "__main__":
    main()
