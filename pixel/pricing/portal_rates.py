import asyncio
import contextlib
import json
from datetime import timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.api.portal import Lane, NotOnPortal, RateLimited
from pixel.contracts import RateOption, Rfq
from pixel.core import events, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import calc, selection

LANES_TTL_S = 600
# Rounds 120 and 121: a rate read that answered 503 is read once more before the quote, inside the desk wait (115 s):
# the outage of 00:19 to 00:56 ended mid-round and the faster airline was lost to the sheet's.
REREAD_AT_S = 105
_lanes: tuple[object, float, list[Lane]] | None = None
_breaks: dict[tuple[str, str], tuple[int, ...]] = {}


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


async def _read(ctx: Context, rfq: Rfq, deadline: Deadline, code: str, name: str) -> RateOption | None:
    try:
        option = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, rfq.weight_kg, rfq.volume_cbm, not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError) as exc:
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "carrier": name, "error": type(exc).__name__, "hint": str(exc)}
                    | ({"page": exc.page} if getattr(exc, "page", None) else {}))  # fmt: skip
        return None
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip
    return option


async def reread(ctx: Context, rfq: Rfq, deadline: Deadline, now: asyncio.Event) -> None:
    """Reads once more, at REREAD_AT_S after the mail or as soon as `now` is set, every selectable airline of the
    lane whose rate read answered 503 and that no portal rate priced since. A rate found is an `option.found` like
    any other; a second 503 is one more `option.failed`. Never waits for the rate limit (`_read`)."""
    at = deadline.received_at + timedelta(seconds=REREAD_AT_S)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(now.wait(), max(0.0, (at - timing.now()).total_seconds()))
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id=? AND kind IN ('option.failed','option.found') ORDER BY id",
        (rfq.session_id,),
    ).fetchall()
    down: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("source") == "portal" and payload.get("error") == "RatesUnavailable":
            down.add(payload.get("carrier"))
        elif (option := payload.get("option")) and option.get("source") == "portal":
            down.discard(option.get("carrier"))
    lane = next((x for x in await lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    todo = [(c, n) for c, n in (lane.airlines if lane else ()) if n in down and selection.requested_carrier(rfq, n, c)]
    remaining = ctx.portal.rate_remaining
    await asyncio.gather(*(_read(ctx, rfq, deadline, c, n) for c, n in todo[: None if remaining is None else remaining]))


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Per airline on the lane one rate read at the actual weight and one at its next break, within the minute's
    read budget. Airlines the portal sends
    elsewhere (404, 503) are reported as `option.failed`; `engine` mails every airline nothing else priced."""
    try:
        lane = next((x for x in await lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    except (ApiError, httpx.HTTPError, ValueError, KeyError) as exc:
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "error": type(exc).__name__})  # fmt: skip
        return []
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
        options = await asyncio.gather(*(_read(ctx, rfq, deadline, code, name) for code, name in batch))
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
