import asyncio

from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events, feedback
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, pickup, portal_rates, selection, store


def _found(ctx: Context, rfq: Rfq, option: RateOption) -> None:
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip


def sheet_options(ctx: Context, rfq: Rfq) -> list[RateOption]:
    found = []
    for tariff in store.lookup(ctx, rfq.origin, rfq.destination, rfq.received_at.date()):
        try:
            option = calc.price_tariff(tariff, rfq)
        except ValueError as exc:
            events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                        payload={"source": "sheet", "carrier": tariff.carrier, "error": str(exc)})  # fmt: skip
            continue
        _found(ctx, rfq, option)
        found.append(option)
    return found


def combine(rfq: Rfq, options: list[RateOption], rule: str = selection.DEFAULT_RULE) -> list[RateOption]:
    """Airline options, each with the balanced pickup added for door-to-port (Q4: the best price includes it).
    Without a pickup price a door-to-port request is priced port-to-port rather than not at all."""
    airline = [o for o in options if o.trucker is None]
    pickups = [o for o in options if o.trucker is not None]
    if not (rfq.door_pickup and pickups):
        return airline
    timed = [o for o in pickups if o.pickup_hours is not None]
    if len(timed) >= 2:
        best = selection.balanced(timed, lambda o: o.pickup_hours, rule)
    else:
        best = min(pickups, key=lambda o: (o.total_eur, o.carrier))
    return [calc.with_pickup(o, best.total_eur, best.trucker).model_copy(update={"pickup_hours": best.pickup_hours})
            for o in airline]  # fmt: skip


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Every priced option for the request: sheets (instant), portal and bids in parallel, pickup added for
    door-to-port. Emits `option.found` per option as it arrives, so the fallback always has the best so far."""
    pickup_task = asyncio.create_task(pickup.options(ctx, rfq, deadline))
    sheets = sheet_options(ctx, rfq)
    portal = await portal_rates.options(ctx, rfq, deadline)
    priced = {o.carrier.casefold() for o in sheets + portal}
    unpriced = [(name, email) for name, email in store.carriers(ctx, "airline", rfq.destination)
                if name.casefold() not in priced]  # fmt: skip
    mailed = await bids.request(ctx, rfq, unpriced) if unpriced else []
    offers = await bids.collect(ctx, rfq.session_id, deadline, len(mailed)) if mailed else []
    pickups = await pickup_task
    found = sheets + portal + [o for o in offers if o.source in (Source.BID, Source.RATEDESK)] + pickups
    return combine(rfq, found, feedback.pickup_rule(ctx))
