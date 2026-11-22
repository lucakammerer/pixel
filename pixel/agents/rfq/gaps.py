"""Known gaps: what the system itself knows it has not read, closed before the quote leaves when it can still
change the quote.

The check (`check.py`) and its repairs (`repair.py`) find faults inside a quote. A quote can be consistent and still
lose: rounds 120 to 123 quoted the sheet airline while the rate API answered 503, and the scorer's best price was a
portal rate we never read. Nothing in such a quote is wrong; what is missing is known only from the session's own
log. `find` lists those gaps:

- `source_down`: an airline of the lane whose portal read failed (503, the page's bot check, a rate limit, a
  network error; not "not on the portal") and that no source priced since;
- `answer_pending`: an airline, the rate desk or a trucker we asked by mail that has not answered;
- `unread_sentences`: sentences no rule reads whose mapping by the model failed (`novelty.check`);
- `fact_missing`: a fact the price needs that the request left open (only reported: `extract._ask` asks for it).

`close` acts on the ones that can still change the quote, and only while time is left: a failed portal read is read
again (at most `REREADS` times, `REREAD_GAP_S` apart), a pending answer is waited for when the session's price basis
is thin or a source is down, the novelty mapping is tried once more. The time is `by`: full time points end at 120 s
(R29), so without a failed source the quote never waits past that; with the rate API down (503: `OUTAGE`; any other
read error is read again inside `by` only), up to the repair margin, since the
rounds a source fails are those few teams win and the board multiplies (`few_winners`). A `quote.gaps` event says what
was found, closed and left open. No model call but the one retried novelty mapping; nothing here ever blocks the
quote: on any error it goes out as it is (R1).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from pixel.agents.rfq import novelty
from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import portal_rates, selection, session

REREADS = 2  # further portal reads per failed airline, after `portal_rates.reread`
REREAD_GAP_S = 6.0  # an outage that ends mid-round ends between two reads this far apart
POLL_S = 0.5
THIN = 2  # fewer priced airlines (or truckers) than this: a pending answer may well be the best price
NOVELTY_MIN_S = 9.0  # a retried mapping needs `novelty.WAIT_S` and a second to spare
NOT_A_GAP = {"NotOnPortal"}  # the portal sends this airline elsewhere: nothing to read again
OUTAGE = {"RatesUnavailable"}  # the rate API's 503 (and the page refusing): only this waits past the full time points


@dataclass(frozen=True)
class Gap:
    kind: str
    name: str  # the airline, trucker or fact
    can_change: bool  # whether closing it can change the quote
    outage: bool = False  # a source down for the rate API's 503: worth time past the full time points


def _payloads(ctx: Context, session_id: int, *kinds: str) -> list[tuple[str, dict, int | None]]:
    marks = ",".join("?" * len(kinds))
    rows = ctx.db.execute(
        f"SELECT kind, payload, message_id FROM events WHERE session_id = ? AND kind IN ({marks}) ORDER BY id",
        (session_id, *kinds),
    ).fetchall()
    return [(r["kind"], json.loads(r["payload"]), r["message_id"]) for r in rows]


def _priced(ctx: Context, rfq: Rfq) -> tuple[set[str], set[str]]:
    """The airlines and truckers the session holds a price of, for this lane."""
    found = session.for_request(ctx, rfq)
    airlines = {o.carrier.casefold() for o in found if o.trucker is None}
    truckers = {o.trucker.casefold() for o in found if o.trucker}
    return airlines, truckers


def find(ctx: Context, rfq: Rfq) -> list[Gap]:
    """Every known gap of the session's request. Never raises: an unreadable log is no gap."""
    try:
        return _find(ctx, rfq)
    except Exception as exc:  # noqa: BLE001
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"where": "gaps", "error": repr(exc)})
        return []


def _find(ctx: Context, rfq: Rfq) -> list[Gap]:
    airlines, truckers = _priced(ctx, rfq)
    gaps: list[Gap] = []
    down: dict[str, tuple[str, str]] = {}
    for kind, p, _ in _payloads(ctx, rfq.session_id, "option.failed"):
        if p.get("source") == "portal" and p.get("carrier") and p.get("error") not in NOT_A_GAP:
            down[p["carrier"].casefold()] = (p["carrier"], str(p.get("error")))
    for key, (name, error) in down.items():
        if key in airlines:
            continue
        usable = selection.requested_carrier(rfq, name) and bool(selection.allowed(rfq, [_named(name)]))
        gaps.append(Gap("source_down", name, usable, error in OUTAGE))
    asked = _payloads(ctx, rfq.session_id, "bid.requested", "bid.received")
    answered = {p.get("request_id") for kind, p, _ in asked if kind == "bid.received"}
    answered_by = {str(p.get("carrier", "")).casefold() for kind, p, _ in asked if kind == "bid.received"}
    for kind, p, message_id in asked:
        if kind != "bid.requested" or message_id in answered or str(p.get("carrier", "")).casefold() in answered_by:
            continue
        if (p.get("origin"), p.get("destination")) != (rfq.origin, rfq.destination):
            continue
        name = str(p.get("carrier") or p.get("email") or "")
        if p.get("pickup"):
            if rfq.door_pickup and name.casefold() not in truckers:
                gaps.append(Gap("answer_pending", name, len(truckers) < THIN))
        elif name.casefold() not in airlines:
            gaps.append(Gap("answer_pending", name, len(airlines) < THIN))
    novel = _payloads(ctx, rfq.session_id, "rfq.novelty")
    if novel and novel[-1][1].get("leftover") and "error" in (novel[-1][1].get("mapped") or {}):
        gaps.append(Gap("unread_sentences", f"{len(novel[-1][1]['leftover'])} sentences", True))
    for fact in rfq.missing:
        if fact in ("origin", "destination", "weight_kg", "volume_cbm", "pickup_postcode"):
            gaps.append(Gap("fact_missing", fact, False))
    return gaps


def _named(name: str) -> RateOption:
    """A stand-in option of the airline `name`, for `selection.allowed`."""
    zero = Decimal(0)
    return RateOption(source=Source.PORTAL, carrier=name, origin="", destination="", chargeable_kg=zero,
                      billed_kg=zero, freight=zero, total=zero, currency="EUR", total_eur=zero, transit_days=0)  # fmt: skip


def _left(by: datetime) -> float:
    return (by - timing.now()).total_seconds()


FULL_TIME_S = 110  # R29: full time points until 120 s; ten left for a translation and the repairs


def by_for(deadline: Deadline) -> datetime:
    """When a quote without a failed source stops closing gaps: the end of the full time points."""
    return min(deadline.received_at + timedelta(seconds=FULL_TIME_S), deadline.hard_at)


async def close(
    ctx: Context, rfq: Rfq, deadline: Deadline, *, by: datetime, outage_by: datetime, text: str = "", subject: str = ""
) -> tuple[Rfq, bool]:
    """Closes what it can of the request's gaps before `by`, or before `outage_by` when a portal read failed for an
    airline that could change the quote. Returns the request (the novelty mapping may rule out an airline) and whether
    anything new arrived, so the caller prices again from the session's options. Emits `quote.gaps` when there was any
    gap. Never raises."""
    started = timing.now()
    mark = ctx.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    try:
        found = find(ctx, rfq)
        if not found:
            return rfq, False
        outage = any(g.kind == "source_down" and g.can_change and g.outage for g in found)
        until = max(by, outage_by) if outage else by
        rereads, retried = 0, False
        while _left(until) > 0:
            gaps = find(ctx, rfq)
            down = [g for g in gaps if g.kind == "source_down" and g.can_change]
            pending = [g for g in gaps if g.kind == "answer_pending" and (g.can_change or outage)]
            unread = [g for g in gaps if g.kind == "unread_sentences"]
            if unread and not retried and _left(until) >= NOVELTY_MIN_S:
                retried = True
                rfq = await novelty.check(ctx, rfq, text, subject)
                continue
            if down and rereads < REREADS and ctx.portal is not None:
                rereads += 1
                await _read_again(ctx, rfq, deadline, until, [g.name for g in down])
                if find(ctx, rfq) and _left(until) > REREAD_GAP_S and rereads < REREADS:
                    await asyncio.sleep(min(REREAD_GAP_S, max(0.0, _left(until) - 1)))
                continue
            if not pending:
                break
            await asyncio.sleep(min(POLL_S, max(0.0, _left(until))))
        left = find(ctx, rfq)
        changed = (
            ctx.db.execute(
                "SELECT 1 FROM events WHERE id > ? AND session_id = ? AND kind IN ('option.found', 'rfq.extracted') LIMIT 1",
                (mark, rfq.session_id),
            ).fetchone()
            is not None
        )
        events.emit(ctx, "quote.gaps", "rfq", session_id=rfq.session_id,
                    payload={"found": [asdict(g) for g in found], "open": [asdict(g) for g in left],
                             "outage": outage, "rereads": rereads, "changed": changed,
                             "waited_s": round((timing.now() - started).total_seconds(), 1)})  # fmt: skip
    except Exception as exc:  # noqa: BLE001  a gap never costs the quote
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "gaps.close", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip
        return rfq, True
    return rfq, changed


async def _read_again(ctx: Context, rfq: Rfq, deadline: Deadline, until: datetime, names: list[str]) -> None:
    """One more portal read of each airline in `names`, never waiting past `until` for the rate limit."""
    lane = next((x for x in await portal_rates.lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)),
                None)  # fmt: skip
    wanted = {n.casefold() for n in names}
    todo = [(c, n) for c, n in (lane.airlines if lane else ()) if n.casefold() in wanted]
    remaining = ctx.portal.rate_remaining
    window = replace(deadline, target_at=until)
    await asyncio.wait_for(
        asyncio.gather(*(portal_rates._read(ctx, rfq, window, c, n) for c, n in todo[: None if remaining is None else remaining])),
        max(0.1, _left(until)),
    )  # fmt: skip
