"""The feedback loop: what each request scored, why it lost points, and what that teaches the next quote.

`run` polls GET /v1/board/me. The board shows one total, so a request's points are the change of that total at the
moment its verdict appears. `explain` works back from the points to the scorer's best price and looks for it among
the options the session had; the result is a `feedback` event in that session. For a door-to-port request it names
the trucker the scorer took, and `pickup_rule` picks the balanced rule that agrees with those most often.
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pixel.contracts import RateOption
from pixel.core import db, events
from pixel.core.context import Context
from pixel.pricing import selection, session

POLL_S = 10
ACCURACY, TIME, BONUS = Decimal(45), Decimal(15), Decimal(10)
FULL_S, EXPIRES_S = 120, 360  # R29
ZERO_AT = Decimal("0.10")  # accuracy reaches 0 at this distance from the best price; fitted on scored requests
FINAL = {"accepted", "rejected", "no answer"}
STATE = "feedback.state"


def time_points(seconds: float) -> Decimal:
    if seconds <= FULL_S:
        return TIME
    return max(Decimal(0), TIME * Decimal(EXPIRES_S - seconds) / Decimal(EXPIRES_S - FULL_S))


def explain(points: Decimal, seconds: float, quoted: RateOption, options: list[RateOption]) -> dict[str, Any]:
    """Accuracy points, the points lost, and the option (airline, trucker) whose total is the scorer's best price.
    The distance from the best price counts in both directions, so each loss fits two best prices; `best` is
    None when neither is a total the session could have quoted."""
    accuracy = points - time_points(seconds)
    lost = ACCURACY - accuracy
    result: dict[str, Any] = {"accuracy": accuracy, "lost": max(lost, Decimal(0)), "best": None}
    if not Decimal(0) <= accuracy <= ACCURACY:
        return result
    if lost < Decimal("0.05"):
        return result | {"best": quoted.total_eur, "carrier": quoted.carrier, "trucker": quoted.trucker}
    off = lost / ACCURACY * ZERO_AT
    airlines = [o for o in options if o.trucker is None]
    pickups: list[RateOption | None] = [o for o in options if o.trucker is not None] if quoted.trucker else []
    for best in (quoted.total_eur / (1 + off), quoted.total_eur / (1 - off)):
        for airline in airlines:
            for pickup in pickups or [None]:
                total = airline.total_eur + (pickup.total_eur if pickup else 0)
                if abs(total - best) <= total * Decimal("0.0001") + Decimal("0.05"):
                    return result | {"best": total, "carrier": airline.carrier, "billed_kg": airline.billed_kg,
                                     "trucker": pickup.trucker if pickup else None}  # fmt: skip
    return result


def pickup_rule(ctx: Context) -> str:
    """The balanced rule (`selection.RULES`) that picks the scorer's trucker in the most explained requests."""
    rows = ctx.db.execute(
        "SELECT session_id, json_extract(payload, '$.trucker') AS trucker FROM events"
        " WHERE kind = 'feedback' AND json_extract(payload, '$.trucker') IS NOT NULL"
    ).fetchall()
    hits = dict.fromkeys(selection.RULES, 0)
    try:
        for row in rows:
            bids = [o for o in session.options(ctx, row["session_id"]) if o.trucker and o.pickup_hours is not None]
            for rule in selection.RULES if len(bids) > 1 else ():
                hits[rule] += selection.balanced(bids, lambda o: o.pickup_hours, rule).trucker == row["trucker"]
    except Exception:  # noqa: BLE001  a stored option we cannot read must not cost the quote: keep the default rule
        return next(iter(selection.RULES))
    return max(hits, key=lambda rule: hits[rule])


def _session(ctx: Context, sent_at: str) -> int | None:
    asked = datetime.fromisoformat(sent_at)
    rows = ctx.db.execute("SELECT id, received_at FROM sessions ORDER BY id DESC LIMIT 100").fetchall()
    near = [(abs((datetime.fromisoformat(r["received_at"]) - asked).total_seconds()), r["id"]) for r in rows]
    return min(near)[1] if near and min(near)[0] <= 10 else None


def _quoted(ctx: Context, session_id: int) -> RateOption | None:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'quote.drafted' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return RateOption.model_validate(json.loads(row["payload"])["option"]) if row else None


def _round(ctx: Context, row: dict[str, Any], delta: Decimal | None) -> None:
    payload: dict[str, Any] = {"round": row["round_no"], "verdict": row["verdict"], "reason": row.get("reason")}
    session_id = _session(ctx, row["sent_at"])
    quoted = _quoted(ctx, session_id) if session_id else None
    if row["verdict"] == "accepted" and quoted and delta is not None and row.get("answered_at"):
        seconds = (datetime.fromisoformat(row["answered_at"]) - datetime.fromisoformat(row["sent_at"])).total_seconds()
        options = session.options(ctx, session_id)
        # A tracking bonus can land in the same poll: prefer the reading of the change that the options explain.
        readings = [explain(p, seconds, quoted, options) | {"points": p} for p in (delta, delta - BONUS)]
        fit = next((r for r in readings if r["best"] is not None), readings[0])
        payload |= {"seconds": round(seconds), "quoted": quoted.total_eur, "quoted_trucker": quoted.trucker} | fit
    events.emit(ctx, "feedback", "board", session_id=session_id,
                payload=json.loads(json.dumps(payload, default=str)))  # fmt: skip


async def once(ctx: Context) -> int:
    """One poll: a `feedback` event for every request whose verdict is new. Returns how many."""
    data = await ctx.board.me()
    state = json.loads(db.get(ctx.db, STATE) or '{"total": null, "verdicts": {}}')
    total = Decimal(str(data.get("total") or 0))
    new = [r for r in data.get("requests", []) if r.get("verdict") in FINAL
           and state["verdicts"].get(str(r["round_no"])) != r["verdict"]]  # fmt: skip
    first = state["total"] is None  # nothing to compare the total with: remember the verdicts, explain nothing
    for row in [] if first else new:
        _round(ctx, row, total - Decimal(state["total"]) if len(new) == 1 else None)
    state["verdicts"].update({str(r["round_no"]): r["verdict"] for r in new})
    db.put(ctx.db, STATE, json.dumps({"total": str(total), "verdicts": state["verdicts"]}))
    return 0 if first else len(new)


async def run(ctx: Context) -> None:
    while True:
        try:
            await once(ctx)
        except Exception as exc:  # noqa: BLE001  feedback must never take the service down
            events.emit(ctx, "error", "board", payload={"where": "feedback", "error": repr(exc)[:200]})
        await asyncio.sleep(POLL_S)
