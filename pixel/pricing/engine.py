import asyncio

from pixel.api.mailbox import RATEDESK
from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, exclusions, pickup, portal_rates, selection, session, store

PORTAL_HEAD_START_S = 1.0


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


def _ask_desk(ctx: Context, rfq: Rfq) -> bool:
    """R21: a rate read for this request answered 503, so the rate API is down; the desk mails the portal's rate per
    airline with its rate id. A 404 or 429 leaves the API up: the desk would only repeat it. Once per request."""
    down = ctx.db.execute(
        "SELECT 1 FROM events WHERE session_id=? AND kind='option.failed' AND json_extract(payload,'$.source')='portal'"
        " AND json_extract(payload,'$.error')='RatesUnavailable'",
        (rfq.session_id,),
    ).fetchone()
    asked = ctx.db.execute(
        "SELECT 1 FROM events WHERE session_id=? AND kind='bid.requested' AND json_extract(payload,'$.email')=?"
        " AND json_extract(payload,'$.origin')=? AND json_extract(payload,'$.destination')=?",
        (rfq.session_id, RATEDESK, rfq.origin, rfq.destination),
    ).fetchone()
    return bool(down) and not asked


def combine(
    rfq: Rfq,
    options: list[RateOption],
    rule: str = selection.DEFAULT_RULE,
    *,
    excluded: exclusions.Exclusions | None = None,
) -> list[RateOption]:
    """Airline options, each with the balanced pickup added for door-to-port (Q4: the best price includes it).
    Without a pickup price a door-to-port request is priced port-to-port rather than not at all."""
    matching = [o for o in options if session.on_lane(o, rfq) and (excluded is None or excluded.allows(o))]
    airline = [o for o in matching if o.trucker is None]
    pickups = [o for o in matching if o.trucker is not None]
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
    excluded = exclusions.for_request(ctx, rfq)
    sheets = [o for o in sheet_options(ctx, rfq) if excluded.allows(o)]
    carriers = store.carriers(ctx, "airline", rfq.destination)
    priced = {o.carrier.casefold() for o in sheets}
    candidates = [
        (name, email) for name, email in carriers if name.casefold() not in priced and not excluded.names(name)
    ]
    offers = []
    async with asyncio.TaskGroup() as group:
        pickup_task = group.create_task(pickup.options(ctx, rfq, deadline))
        portal_task = group.create_task(portal_rates.options(ctx, rfq, deadline))
        # Fast portal prices save unnecessary mails. An outage or a slow rate read must still
        # leave mail-only airlines time to answer, including after a late restart.
        head_start = min(PORTAL_HEAD_START_S, max(0.0, (deadline.target_at - timing.now()).total_seconds()))
        await asyncio.wait({portal_task}, timeout=head_start)
        priced.update(o.carrier.casefold() for o in session.for_request(ctx, rfq) if not o.trucker)
        unpriced = [(name, email) for name, email in candidates if name.casefold() not in priced]
        if _ask_desk(ctx, rfq):
            unpriced.append(("Rate desk", RATEDESK))
        if unpriced:
            mailed = await bids.request(ctx, rfq, unpriced)
            if mailed:
                offers = await bids.collect(ctx, rfq.session_id, deadline, mailed)
        pickups = await pickup_task
        offers = [o for o in offers if o.source in (Source.BID, Source.RATEDESK)]
        published = [o for o in session.for_request(ctx, rfq) if o.source == Source.PORTAL and excluded.allows(o)]
        if (sheets or offers or published) and (not rfq.door_pickup or pickups) and not portal_task.done():
            await asyncio.wait({portal_task}, timeout=max(0.0, (deadline.target_at - timing.now()).total_seconds()))
            if not portal_task.done():
                portal_task.cancel()
    portal = [] if portal_task.cancelled() else portal_task.result()
    # Also reuse same-lane bids published before recovery or while another price source was still running.
    carried = [o for o in session.for_request(ctx, rfq) if o.source in (Source.PORTAL, Source.BID, Source.RATEDESK)]
    all_options = dict.fromkeys(sheets + portal + offers + pickups + carried)
    return combine(rfq, list(all_options), feedback.pickup_rule(ctx), excluded=excluded)
