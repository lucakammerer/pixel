import asyncio
import contextlib
import json
from datetime import timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.api.portal import Lane, NotOnPortal, RateLimited, RatesUnavailable
from pixel.contracts import RateOption, Rfq
from pixel.core import events, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import calc, rate_card, selection

LANES_TTL_S = 600
# Rounds 120 and 121: a rate read that answered with an outage is read again before the quote. The first read is
# often inside the outage (00:19 to 00:56 ended mid-round) and the faster airline was lost to the sheet's.
PROBE_AT_S = (15.0, 45.0, 75.0)
FINAL_PROBE_S = 105.0  # the last read leaves `bids.DESK_CUTOFF_S` (115 s) to compose and send
NOT_AN_OUTAGE = frozenset({"NotOnPortal", "RateLimited"})  # the airline or the quota, not the portal
_lanes: tuple[object, float, list[Lane]] | None = None
_breaks: dict[tuple[str, str], tuple[int, ...]] = {}


def _outage(exc: Exception) -> bool:
    """The portal failed for every airline, not for this one (404) or the quota (429)."""
    return isinstance(exc, RatesUnavailable) or not isinstance(exc, (NotOnPortal, RateLimited))


def _outage_name(error: str | None) -> bool:
    """The same, for the name `_read` writes into `option.failed`: `type(exc).__name__`."""
    return bool(error) and error not in NOT_AN_OUTAGE


async def lanes(ctx: Context, *, refresh: bool = False) -> list[Lane]:
    """Ports are free to read; cached for LANES_TTL_S, `refresh=True` after a failed quote."""
    global _lanes
    now = asyncio.get_running_loop().time()
    if refresh or _lanes is None or _lanes[0] is not ctx.portal or now - _lanes[1] > LANES_TTL_S:
        _lanes = (ctx.portal, now, await ctx.portal.lanes())
    return _lanes[2]


async def _at_next_break(ctx: Context, rfq: Rfq, deadline: Deadline, code: str, option: RateOption) -> RateOption:
    """R13 on the portal: the airline's next weight break, billed in full, can cost less than the actual weight in
    its own bracket, and the best price we are scored against bills it. Breaks are free to read; the rate at the
    break is one more counted read. Any failure keeps the option we have."""
    try:
        version = option.dataset_version
        if version is None:
            breaks = await ctx.portal.brackets(code)
        else:
            key = (code, version)
            if key not in _breaks:
                _breaks[key] = await ctx.portal.brackets(code)
            breaks = _breaks[key]
        higher = next((b for b in breaks if b > option.chargeable_kg), None)
        if higher is None:
            return option
        at_break = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, Decimal(higher), Decimal(0), not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError):
        return option
    if at_break.dataset_version != option.dataset_version or at_break.total_eur >= option.total_eur:
        return option
    return at_break.model_copy(update={"chargeable_kg": option.chargeable_kg})


async def _read(
    ctx: Context, rfq: Rfq, deadline: Deadline, code: str, name: str, down: asyncio.Event | None = None
) -> RateOption | None:
    try:
        option = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, rfq.weight_kg, rfq.volume_cbm, not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError) as exc:
        if down is not None and _outage(exc):
            down.set()
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "carrier": name, "error": type(exc).__name__, "hint": str(exc)}
                    | ({"page": exc.page} if getattr(exc, "page", None) else {}))  # fmt: skip
        # a 429 that outlasts the deadline prices from the card too: every read 429 lost 56 of 60 drilled rounds
        return _card(ctx, rfq, code) if _outage(exc) or isinstance(exc, RateLimited) else None
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip
    return option


def _card(ctx: Context, rfq: Rfq, code: str) -> RateOption | None:
    try:
        card = rate_card.option(ctx, rfq, code)
    except Exception as exc:  # noqa: BLE001  the card's failure must not take the TaskGroup's other sources with it
        events.emit(ctx, "error", "pricing", session_id=rfq.session_id,
                    payload={"where": "rate_card", "error": type(exc).__name__})  # fmt: skip
        return None
    if card is not None:
        events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                    payload={"option": card.model_dump(mode="json"), "why": "rate card"})  # fmt: skip
    return card


def _unpriced(ctx: Context, rfq: Rfq) -> tuple[set[str | None], bool]:
    """The airlines whose rate read answered with an outage and that no portal rate priced since, and whether the
    lane itself could not be listed (no carrier to name)."""
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id=? AND kind IN ('option.failed','option.found') ORDER BY id",
        (rfq.session_id,),
    ).fetchall()
    down: set[str | None] = set()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("source") == "portal" and _outage_name(payload.get("error")):
            down.add(payload.get("carrier"))
        elif (option := payload.get("option")) and option.get("source") == "portal" and option.get("rate_id"):
            down.discard(option.get("carrier"))  # a price without a rate id (a rate card) is no answer of the API
    return down, None in down


async def probe(ctx: Context, rfq: Rfq, deadline: Deadline) -> bool:
    """Reads again every selectable airline of the lane whose rate read answered with an outage and that no rate
    priced since. A rate found is an `option.found` like any other; a second failure is one more `option.failed`.
    True when the API priced an airline (a rate id), so the caller stops probing. Never waits for the rate limit (`_read`)."""
    down, unlisted = _unpriced(ctx, rfq)
    if not down:
        return False
    try:
        lane = next((x for x in await lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    except (ApiError, httpx.HTTPError, ValueError, KeyError):
        return False
    todo = [
        (c, n) for c, n in (lane.airlines if lane else ())
        if (unlisted or n in down) and selection.requested_carrier(rfq, n, c)
    ]  # fmt: skip
    remaining = ctx.portal.rate_remaining
    found = await asyncio.gather(
        *(_read(ctx, rfq, deadline, c, n) for c, n in todo[: None if remaining is None else remaining])
    )
    return any(option is not None and option.rate_id for option in found)


async def probes(ctx: Context, rfq: Rfq, deadline: Deadline, down: asyncio.Event) -> None:
    """Rounds 120 and 121: waits for the first rate read to fail, then reads the airlines it could not price again
    at PROBE_AT_S after the mail, and once more at FINAL_PROBE_S when the bids have not come in by then. The outage
    of 00:19 to 00:56 ended mid-round and the faster airline was lost to the sheet's."""
    cutoff = min(deadline.received_at + timedelta(seconds=FINAL_PROBE_S), deadline.hard_at)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(down.wait(), max(0.0, (cutoff - timing.now()).total_seconds()))
    if not down.is_set():
        return
    for at in PROBE_AT_S:
        if timing.now() >= cutoff:
            return
        wait = (deadline.received_at + timedelta(seconds=at) - timing.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)
        if await probe(ctx, rfq, deadline):
            return
    wait = (cutoff - timing.now()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    await probe(ctx, rfq, deadline)


async def options(ctx: Context, rfq: Rfq, deadline: Deadline, *, down: asyncio.Event | None = None) -> list[RateOption]:
    """Per airline on the lane one rate read at the actual weight and one at its next break, within the minute's
    read budget. Airlines the portal sends
    elsewhere (404) and an outage answer for every airline (5xx, 403, a timeout) are reported as `option.failed`;
    `engine` mails every airline nothing else priced. `down` is set on the first outage answer, so the desk and the
    probes do not wait for a sample."""
    try:
        lane = next((x for x in await lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    except (ApiError, httpx.HTTPError, ValueError, KeyError) as exc:
        if down is not None:
            down.set()
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "error": type(exc).__name__})  # fmt: skip
        # The lane list fails with the rates (rounds 46, 47): the card still knows the lane's airlines.
        return [o for c in rate_card.airlines(ctx, rfq.origin, rfq.destination) if (o := _card(ctx, rfq, c))]
    if lane is None:
        return []

    # Check the quota again after each batch. A zero left over from a previous minute permits one
    # probe; Portal.rate handles Retry-After if the window really is exhausted.
    pending = sorted(lane.airlines, key=lambda airline: not selection.requested_carrier(rfq, airline[1], airline[0]))
    found = []
    while pending:
        remaining = ctx.portal.rate_remaining
        slots = len(pending) if remaining is None else max(1, remaining)
        batch, pending = pending[:slots], pending[slots:]
        options = await asyncio.gather(*(_read(ctx, rfq, deadline, code, name, down) for code, name in batch))
        found.extend((code, o) for (code, _), o in zip(batch, options, strict=True) if o is not None)
    if not calc.BILL_HIGHER_BREAK:
        return [option for _, option in found]

    # Publish each actual-weight price before spending any reads on higher breaks. The fallback can
    # use it even while an optional second read is waiting for the next window.
    improved = []
    for code, option in found:
        best = await _at_next_break(ctx, rfq, deadline, code, option)
        if best is not option:
            events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                        payload={"option": best.model_dump(mode="json")})  # fmt: skip
        improved.append(best)
    return improved


async def refresh(ctx: Context, option: RateOption) -> RateOption:
    """A fresh rate_id for the same airline and lane, for a booking after 410."""
    airline = option.carrier_code or option.carrier
    return await ctx.portal.rate(option.origin, option.destination, airline, option.billed_kg, Decimal(0))
