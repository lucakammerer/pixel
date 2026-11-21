import asyncio
import time
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.api.portal import Lane, NotOnPortal, RateLimited
from pixel.contracts import RateOption, Rfq
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import calc

LANES_TTL_S = 600
_lanes: tuple[float, list[Lane]] | None = None
_breaks: dict[str, tuple[int, ...]] = {}


async def lanes(ctx: Context, *, refresh: bool = False) -> list[Lane]:
    """Ports are free to read; cached for LANES_TTL_S, `refresh=True` after a failed quote."""
    global _lanes
    if refresh or _lanes is None or time.monotonic() - _lanes[0] > LANES_TTL_S:
        _lanes = (time.monotonic(), await ctx.portal.lanes())
    return _lanes[1]


async def _at_next_break(ctx: Context, rfq: Rfq, deadline: Deadline, code: str, option: RateOption) -> RateOption:
    """R13 on the portal: the airline's next weight break, billed in full, can cost less than the actual weight in
    its own bracket, and the best price we are scored against bills it. Breaks are free to read; the rate at the
    break is one more counted read. Any failure keeps the option we have."""
    try:
        if code not in _breaks:
            _breaks[code] = await ctx.portal.brackets(code)
        higher = next((b for b in _breaks[code] if b > option.chargeable_kg), None)
        if higher is None:
            return option
        at_break = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, Decimal(higher), Decimal(0), not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError):
        return option
    if at_break.total_eur >= option.total_eur:
        return option
    return at_break.model_copy(update={"chargeable_kg": option.chargeable_kg})


async def _read(ctx: Context, rfq: Rfq, deadline: Deadline, code: str, name: str) -> RateOption | None:
    try:
        option = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, rfq.weight_kg, rfq.volume_cbm, not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError) as exc:
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "carrier": name, "error": type(exc).__name__, "hint": str(exc)})  # fmt: skip
        return None
    if calc.BILL_HIGHER_BREAK:
        option = await _at_next_break(ctx, rfq, deadline, code, option)
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip
    return option


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Per airline on the lane one rate read at the actual weight and one at its next break, within the minute's
    read budget. Airlines the portal sends
    elsewhere (404, 503) are reported as `option.failed`; `engine` mails every airline nothing else priced."""
    lane = next((x for x in await lanes(ctx) if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    if lane is None:
        return []
    airlines = list(lane.airlines)
    budget = None if ctx.portal.rate_remaining is None else max(ctx.portal.rate_remaining, 1)
    if budget is not None and budget < len(airlines):
        for code, name in airlines[budget:]:
            events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                        payload={"source": "portal", "carrier": name, "error": "read budget"})  # fmt: skip
        airlines = airlines[:budget]
    found = await asyncio.gather(*(_read(ctx, rfq, deadline, code, name) for code, name in airlines))
    return [o for o in found if o is not None]


async def refresh(ctx: Context, option: RateOption) -> RateOption:
    """A fresh rate_id for the same airline and lane, for a booking after 410."""
    airline = option.carrier_code or option.carrier
    return await ctx.portal.rate(option.origin, option.destination, airline, option.chargeable_kg, Decimal(0))
