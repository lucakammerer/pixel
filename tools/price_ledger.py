"""Axis P: every scored round, the scorer's best against our quote then and against today's code, offline.

uv run python -m tools.price_ledger <database>                 # rounds lost on price, rounds that lost accuracy, and
                                                               # every accepted round today's code quotes otherwise;
                                                               # the last line counts every scored round
uv run python -m tools.price_ledger <database> --rounds 91,130  # just these

Per scored round (its last `score` verdict; `feedback` names the session and, for an accepted round, the best price
it works back to) today's `engine.combine` and `selection.select` price the options the session had when its quote
was drafted (as `tools/replay_gate.py`), plus what today's code adds that the session lacked:

- `card`: the rate card's price (`rate_card.option`, the whole database) for each portal airline whose read answered
  with an outage, or for every airline on the card when the lane list failed;
- `memory`: the pickup memory (`pickup.estimates`) for a door request no trucker priced, without the session's own
  trucker answers;
- `insurance`: the premium on the terms the request states (`agent._insurance`);
- `read`: today's reading of the request mail by rules; when it differs on the lane, weight, volume or pickup, the
  round is priced again from what is offline: sheets valid on the ready day, the card, the pickup memory.

A rejected round names no best price, only its side: `too_low` puts the best above quoted / 0.95, `too_high` below
quoted / 1.05. Today's total is within 5 % of every best in [today / 1.05, today / 0.95]; `passes if best in` is that
range cut to what the verdict allows, `fails` when nothing is left. Never writes to the database it is given.
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

from pixel.agents.rfq import agent, compose, extract
from pixel.api.mailbox import to_message
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Quote, RateOption, Rfq
from pixel.core import feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import calc, engine, pickup, portal_rates, rate_card, selection, session, store
from tools import replay
from tools.replay_gate import FIELDS, _events, _known

PRICED_BY = ("origin", "destination", "weight_kg", "volume_cbm", "door_pickup", "pickup_postcode", "pickup_zone")
LOW, HIGH = Decimal("0.95"), Decimal("1.05")


def scored(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per round its last verdict, reason, session and the feedback's best price, carrier and trucker."""
    rounds: dict[int, dict] = {}
    for kind, session_id, payload in conn.execute(
        "SELECT kind, session_id, payload FROM events WHERE kind IN ('score', 'feedback') ORDER BY id"
    ):
        data = json.loads(payload)
        row = rounds.setdefault(data["round"], {"round": data["round"]})
        if kind == "score":
            row |= {"verdict": data["verdict"], "reason": data.get("reason"),
                    "accuracy": (data.get("points") or {}).get("accuracy")}  # fmt: skip
        elif session_id is not None:
            row |= {"session": session_id} | {k: data.get(k) for k in ("best", "carrier", "trucker")}
    return rounds


def _draft(conn: sqlite3.Connection, session_id: int) -> tuple[Quote, dict, int] | None:
    drafts = _events(conn, session_id, "quote.drafted")
    sent = _events(conn, session_id, "quote.sent")
    if not drafts:
        return None
    quote_id = json.loads(sent[-1]["payload"])["quote"] if sent else json.loads(drafts[0]["payload"])["quote_id"]
    draft = next((d for d in reversed(drafts) if json.loads(d["payload"])["quote_id"] == quote_id), drafts[0])
    row = conn.execute("SELECT data FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        return None
    stored = json.loads(row["data"])
    quote = Quote.model_validate(_known(Quote, stored) | {"rfq": _known(Rfq, stored["rfq"]),
                                                          "option": session.option(stored["option"])})  # fmt: skip
    return quote, json.loads(draft["payload"]), draft["id"]


async def _reading(ctx: Context, session_id: int) -> Rfq | str:
    """Today's reading of the request mail by rules, or why there is none offline."""
    row = ctx.db.execute("SELECT data FROM messages WHERE id = ? AND direction = 'in'", (session_id,)).fetchone()
    if row is None:
        return "no stored request"
    raw = json.loads(row["data"])["raw"]
    if raw.get("attachments"):
        return "its attachments are not stored"
    message = to_message(raw, "in")
    token = timing.clock.set(lambda: message.sent_at)
    try:
        rfq = (await extract.read_request(ctx, message, deadline_for(message.sent_at, ctx.settings)))[0]
    except Exception as e:  # noqa: BLE001  offline the model's call fails: the rules do not read this request
        return f"the rules leave it to the model ({type(e).__name__})"
    finally:
        timing.clock.reset(token)
    return rfq if rfq.extracted_by != "guess" else "the rules leave it to the model (offline: a guess)"


def _cards(ctx: Context, conn: sqlite3.Connection, session_id: int, rfq: Rfq, before: int,
           have: list[RateOption]) -> list[RateOption]:  # fmt: skip
    failed = [json.loads(e["payload"]) for e in _events(conn, session_id, "option.failed", before)]
    # before `RatesUnavailable` existed a 503 was stored as NotOnPortal with the API's hint (rounds 27, 28)
    down = {f.get("carrier") for f in failed if f.get("source") == "portal" and (
        portal_rates._outage_name(f.get("error")) or "rate API is unavailable" in (f.get("hint") or ""))}  # fmt: skip
    priced = {o.carrier for o in have if o.source.value == "portal" and o.rate_id}
    cards = [
        c for code in rate_card.airlines(ctx, rfq.origin, rfq.destination) if (c := rate_card.option(ctx, rfq, code))
    ]
    return [c for c in cards if (None in down or c.carrier in down) and c.carrier not in priced]


def _memory(conn: sqlite3.Connection, ctx: Context, rfq: Rfq, found: list[RateOption]) -> list[RateOption]:
    conn.execute("SAVEPOINT ledger")
    try:
        conn.execute("DELETE FROM events WHERE session_id = ? AND kind = 'option.found'"
                     " AND json_extract(payload, '$.option.trucker') IS NOT NULL", (rfq.session_id,))  # fmt: skip
        return pickup.estimates(ctx, rfq, found)
    finally:
        conn.execute("ROLLBACK TO ledger")
        conn.execute("RELEASE ledger")


def _same(stored: object, today: object) -> bool:
    if isinstance(today, (list, tuple)) or isinstance(stored, list):
        return list(stored or []) == list(today or [])
    if isinstance(today, Decimal) and stored is not None:
        return round(Decimal(str(stored)), 2) == round(today, 2)
    return str(stored or "") == str(today or "")


def today(ctx: Context, session_id: int, rule: str, reading: Rfq | str) -> tuple[RateOption | None, list[str]]:
    conn = ctx.db
    drafted = _draft(conn, session_id)
    if drafted is None:
        return None, ["no drafted quote"]
    quote, payload, draft_id = drafted
    rfq, notes = quote.rfq, []
    vetoed = [json.loads(v["payload"]) for v in _events(conn, session_id, "option.vetoed", draft_id)]
    found = [(o["id"], session.option(json.loads(o["payload"])["option"]))
             for o in _events(conn, session_id, "option.found", draft_id)]  # fmt: skip
    found = [(i, o) for i, o in found if not any(session.vetoed(o, v) for v in vetoed)]
    readings = _events(conn, session_id, "rfq.extracted", draft_id)
    change = [e["id"] for e in readings if e["message_id"] != session_id]  # `agent.requote`, as `replay_gate.priced`
    found = [o for i, o in found if i > change[-1]] or [o for _, o in found] if change else [o for _, o in found]
    first = next((json.loads(e["payload"]) for e in reversed(readings) if e["message_id"] == session_id), {})
    requoted = any(str(first.get(f)) != str(getattr(rfq, f)) for f in PRICED_BY if f in first)
    if isinstance(reading, Rfq):
        changed = [f for f in FIELDS if f in first and not _same(first[f], getattr(reading, f))]
        notes += [f"read: {f} {first[f]} -> {getattr(reading, f)}" for f in changed]
        if requoted and changed:
            notes.append("quoted on a later mail's change: today's reading of the request not priced")
        elif any(f in PRICED_BY for f in changed):
            truckers = [o for o in found if o.trucker and reading.pickup_zone == rfq.pickup_zone]
            rfq = reading.model_copy(update={"session_id": session_id})
            found = truckers + engine._priced(ctx, rfq, store.lookup(ctx, rfq.origin, rfq.destination, calc.ready_day(rfq)),
                                   emit=False)  # fmt: skip
            found += [c for code in rate_card.airlines(ctx, rfq.origin, rfq.destination)
                      if (c := rate_card.option(ctx, rfq, code))]  # fmt: skip
            notes.append("priced again offline: sheets, card")
        elif changed:
            rfq = rfq.model_copy(update={f: getattr(reading, f) for f in changed})
    else:
        notes.append(f"read: {reading}")
    if rfq.door_pickup and not any(o.trucker and session.on_lane(o, rfq) for o in found):
        moved = [
            o.model_copy(update={"origin": rfq.origin, "destination": rfq.destination}) for o in found if o.trucker
        ]
        if moved:
            notes.append("trucker bids stored for another airport, taken as they were quoted")
            found = [o for o in found if not o.trucker] + moved
    cards = _cards(ctx, conn, session_id, rfq, draft_id, found)
    notes += [f"card {c.carrier} {c.total_eur} {c.transit_days} d" for c in cards]
    found += cards
    if rfq.door_pickup and not any(o.trucker for o in found):
        memory = _memory(conn, ctx, rfq, found)
        notes += [f"memory {m.trucker} {m.total_eur} {m.pickup_hours} h" for m in memory]
        found += memory
    options = engine.combine(rfq, found, rule)
    if rfq.door_pickup:
        options = [o for o in options if o.trucker] or options
    if not options:
        return None, [*notes, "no option prices it"]
    required = payload.get("required") or {}
    arrive_by = date.fromisoformat(required["arrive_by"]) if not rfq.arrive_by and required.get("arrive_by") else None
    pick = selection.select(rfq, options, only=required.get("only_airlines") or (), arrive_by=arrive_by,
                            ready=calc.ready_day(rfq) if arrive_by else None)  # fmt: skip
    premium = agent._insurance(ctx, rfq, compose.customer_body(ctx, rfq.message_id), options, pick)
    if premium:
        pick = calc.with_insurance(pick, premium)
        notes.append(f"insurance {premium}")
    return pick, notes


def window(row: dict, quoted: Decimal, total: Decimal) -> str:
    """The scorer's best prices for which today's total is within 5 %, cut to what the verdict allows."""
    lo, hi = total / HIGH, total / LOW
    if row["verdict"] == "accepted" and (row.get("best") or row.get("accuracy") is not None):
        if row.get("best"):
            bests = [Decimal(row["best"])]
        else:  # the two best prices the points allow (`feedback.explain`)
            off = (feedback.ACCURACY - Decimal(str(row["accuracy"]))) / feedback.ACCURACY * feedback.ZERO_AT
            bests = [quoted / (1 + off), quoted / (1 - off)]
        best = min(bests, key=lambda b: abs(total - b))
        return f"{'passes' if lo <= best <= hi else 'fails'}, {(total - best) / best * 100:+.2f} % from the best {best:.2f}"
    if row.get("reason") == "too_low":
        lo = max(lo, quoted / LOW)
    elif row.get("reason") == "too_high":
        hi = min(hi, quoted / HIGH)
    return f"passes if best in {lo:.2f} to {hi:.2f}" if lo <= hi else "fails"


async def ledger(db: str, rounds: list[int] | None) -> None:
    with tempfile.TemporaryDirectory() as folder:
        config = dataclasses.replace(settings(), dry_run=True, api_key="", db_path=str(Path(folder) / "ledger.sqlite3"))
        replay._copy(db, config.db_path, [])
        ctx = build_context(config)
        await ctx.mailbox.client.aclose()
        offline = httpx.AsyncClient(base_url=config.api_base, transport=httpx.MockTransport(replay._offline))
        ctx.mailbox.client = ctx.portal.client = ctx.gateway.client = ctx.board.client = offline
        rule = feedback.pickup_rule(ctx)
        print("| Round | Verdict | Wish | Quoted then | Scorer's best | Today | Today | What today's code changes |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        all_rows = scored(ctx.db)
        count = dict.fromkeys(("accepted, quoted as sent", "accepted, quoted otherwise", "no session or quote"), 0)
        for number in sorted(
            all_rows, reverse=True
        ):  # newest first: a reading left in the copy is never an earlier one's
            row = all_rows[number]
            lost = row.get("reason") in ("too_low", "too_high") or (
                row.get("verdict") == "accepted" and row.get("accuracy") is not None
                and Decimal(str(row["accuracy"])) < Decimal("44.95"))  # fmt: skip
            if (rounds and number not in rounds) or not (rounds or lost or row["verdict"] == "accepted"):
                continue
            drafted = _draft(ctx.db, row["session"]) if row.get("session") else None
            if drafted is None:
                count["no session or quote"] += 1
                continue
            then = drafted[0].option
            try:
                pick, notes = today(ctx, row["session"], rule, await _reading(ctx, row["session"]))
            except Exception as e:  # noqa: BLE001  a stored option today's contracts cannot read
                pick, notes = None, [f"{type(e).__name__}: {str(e).splitlines()[0]}"]
            same = pick is not None and (pick.total_eur, pick.carrier, pick.trucker) == (
                then.total_eur,
                then.carrier,
                then.trucker,
            )
            if row["verdict"] == "accepted" and not lost:
                count["accepted, quoted as sent" if same else "accepted, quoted otherwise"] += 1
            if not (rounds or lost) and same:
                continue
            best = (
                f"{row['best']} {row.get('carrier') or ''} {row.get('trucker') or ''}".strip()
                if row.get("best")
                else "not given"
            )
            verdict = f"{row['verdict']} {row.get('reason') or ''}".strip() + (
                f" ({row['accuracy']})" if row.get("accuracy") and not row.get("reason") else ""
            )
            quoted = f"{then.total_eur} {then.carrier} {then.trucker or ''} {then.transit_days} d".replace("  ", " ")
            if pick is None:
                shown, verdict_today = "none", "fails"
            else:
                shown = f"{pick.total_eur} {pick.carrier} {pick.trucker or ''} {pick.transit_days} d".replace("  ", " ")
                verdict_today = "same" if same and lost else window(row, then.total_eur, pick.total_eur)
            print(f"| {number} | {verdict} | {drafted[0].rfq.preference.value} | {quoted} | {best} | {shown} |"
                  f" {verdict_today} | {'; '.join(notes)} |")  # fmt: skip
        verdicts = [r.get("verdict") for r in all_rows.values()]
        print(f"\n{len(all_rows)} scored rounds, {verdicts.count('accepted')} accepted;"
              + "".join(f" {name}: {n};" for name, n in count.items())
              + f" lost on price: {sum(r.get('reason') in ('too_low', 'too_high') for r in all_rows.values())}")  # fmt: skip
        await offline.aclose()
        ctx.db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db")
    parser.add_argument("--rounds", help="comma-separated round numbers; default: lost and changed rounds")
    args = parser.parse_args()
    asyncio.run(ledger(args.db, [int(r) for r in args.rounds.split(",")] if args.rounds else None))


if __name__ == "__main__":
    main()
