import asyncio
import time
from decimal import Decimal

from pixel.api.client import ApiError
from pixel.api.portal import Lane, NotOnPortal, RateLimited
from pixel.contracts import RateOption, Rfq
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline

LANES_TTL_S = 600
_lanes: tuple[float, list[Lane]] | None = None


async def lanes(ctx: Context, *, refresh: bool = False) -> list[Lane]:
    """Ports are free to read; cached for LANES_TTL_S, `refresh=True` after a failed quote."""
    global _lanes
    if refresh or _lanes is None or time.monotonic() - _lanes[0] > LANES_TTL_S:
        _lanes = (time.monotonic(), await ctx.portal.lanes())
    return _lanes[1]


async def _read(ctx: Context, rfq: Rfq, deadline: Deadline, code: str, name: str) -> RateOption | None:
    try:
        option = await ctx.portal.rate(
            rfq.origin, rfq.destination, code, rfq.weight_kg, rfq.volume_cbm, not_after=deadline.target_at
        )
    except (NotOnPortal, RateLimited, ApiError) as exc:
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    payload={"source": "portal", "carrier": name, "error": type(exc).__name__, "hint": str(exc)})  # fmt: skip
        return None
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip
    return option


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """One rate read per airline on the lane, within the minute's read budget. Airlines the portal sends
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
