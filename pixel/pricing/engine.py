import asyncio
from decimal import Decimal

from pixel.api.mailbox import RATEDESK
from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, pickup, portal_rates, selection, session, store

PORTAL_HEAD_START_S = 1.0
# Ask a 503 airline for its next weight break only when the shipment fills this share of it: further below, the
# break's lower rate rarely makes up for the extra kilograms (round 27: 859 kg billed at 1000).
BREAK_ASK_SHARE = Decimal("0.65")


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


def _down(ctx: Context, rfq: Rfq) -> set[str]:
    """Airlines (casefolded) whose rate read for this request answered 503."""
    rows = ctx.db.execute(
        "SELECT json_extract(payload,'$.carrier') FROM events WHERE session_id=? AND kind='option.failed' "
        "AND json_extract(payload,'$.source')='portal' AND json_extract(payload,'$.error')='RatesUnavailable'",
        (rfq.session_id,),
    ).fetchall()
    return {row[0].casefold() for row in rows if row[0]}


def _ports(ctx: Context, rfq: Rfq) -> tuple[list[tuple[str, str]], set[str]] | None:
    """(code, name) of the airlines the portal lists on this lane, and the names (casefolded) it lists on any
    other lane only. None while the portal task has not read /ports, or could not: then nothing is skipped on its
    account and no break is asked."""
    if (listed := portal_rates.cached_lanes(ctx)) is None:
        return None
    lane = next((x for x in listed if (x.origin, x.destination) == (rfq.origin, rfq.destination)), None)
    here = list(lane.airlines) if lane else []
    elsewhere = {name.casefold() for x in listed if x is not lane for _, name in x.airlines}
    return here, elsewhere - {name.casefold() for _, name in here}


def _skip(ctx: Context, rfq: Rfq, unpriced: list[tuple[str, str]], ports, has_option: bool) -> set[str]:
    """Airlines not worth a mail: on the portal for other lanes only, or their decline in this session or on the
    same dataset lists the lanes they fly without this one. Never one whose rate read on this lane answered 503,
    and nothing while no airline has priced the request yet."""
    if not has_option:
        return set()
    here = {name.casefold() for _, name in ports[0]} if ports else set()
    skip = set(ports[1]) if ports else set()
    skip |= bids.not_serving(ctx, rfq)
    skip -= here & _down(ctx, rfq)
    return {name.casefold() for name, _ in unpriced} & skip


async def _break_asks(
    ctx: Context, rfq: Rfq, deadline: Deadline, asked: list[tuple[str, str]], ports
) -> list[tuple[str, str, int]]:
    """R13 while the rate API is down: one more request per airline whose rate read answered 503 (and the rate
    desk), at the airline's next weight break when the shipment is close enough to it to make it cheaper."""
    if not ports or timing.now() >= deadline.target_at:
        return []
    down = _down(ctx, rfq)
    chargeable = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    emails = {name.casefold(): email for name, email in asked}
    codes = [(code, name) for code, name in ports[0] if name.casefold() in down]
    if not codes:
        return []
    remaining = max(0.0, (deadline.target_at - timing.now()).total_seconds())
    try:
        breaks = await asyncio.wait_for(
            asyncio.gather(*(portal_rates.next_break_for(ctx, code, chargeable) for code, _ in codes)), remaining
        )
    except TimeoutError:
        return []
    near = {name: b for (_, name), b in zip(codes, breaks, strict=True) if b and chargeable / b >= BREAK_ASK_SHARE}
    asks = [(name, emails[name.casefold()], b) for name, b in near.items() if name.casefold() in emails]
    if RATEDESK in emails.values() and near:
        asks.append(("Rate desk", RATEDESK, min(near.values())))
    return [ask for ask in asks if not _asked_at_break(ctx, rfq, ask[1])]


def _asked_at_break(ctx: Context, rfq: Rfq, email: str) -> bool:
    return (
        ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='bid.requested' AND json_extract(payload,'$.email')=? "
            "AND json_extract(payload,'$.requested_kg') IS NOT NULL AND json_extract(payload,'$.origin')=? "
            "AND json_extract(payload,'$.destination')=?",
            (rfq.session_id, email, rfq.origin, rfq.destination),
        ).fetchone()
        is not None
    )


def combine(rfq: Rfq, options: list[RateOption], rule: str = selection.DEFAULT_RULE) -> list[RateOption]:
    """Airline options, each with the balanced pickup added for door-to-port (Q4: the best price includes it).
    Without a pickup price a door-to-port request is priced port-to-port rather than not at all."""
    matching = [o for o in options if session.on_lane(o, rfq)]
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
    sheets = sheet_options(ctx, rfq)
    carriers = store.carriers(ctx, "airline", rfq.destination)
    priced = {o.carrier.casefold() for o in sheets}
    candidates = [(name, email) for name, email in carriers if name.casefold() not in priced]
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
        ports = _ports(ctx, rfq)
        skipped = _skip(ctx, rfq, unpriced, ports, bool(priced)) if unpriced else set()
        for name, _ in unpriced:
            if name.casefold() in skipped:
                events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                            payload={"source": "mail", "carrier": name, "error": "not mailed: does not fly the lane"})  # fmt: skip
        unpriced = [(name, email) for name, email in unpriced if name.casefold() not in skipped]
        if _ask_desk(ctx, rfq):
            unpriced.append(("Rate desk", RATEDESK))
        if unpriced:
            mailed = await bids.request(ctx, rfq, unpriced)
            for name, email, kg in await _break_asks(ctx, rfq, deadline, unpriced, ports):
                mailed += await bids.request(ctx, rfq, [(name, email)], requested_kg=Decimal(kg))
            if mailed:
                offers = await bids.collect(ctx, rfq.session_id, deadline, mailed)
        pickups = await pickup_task
        offers = [o for o in offers if o.source in (Source.BID, Source.RATEDESK)]
        published = [o for o in session.for_request(ctx, rfq) if o.source == Source.PORTAL]
        if (sheets or offers or published) and (not rfq.door_pickup or pickups) and not portal_task.done():
            await asyncio.wait({portal_task}, timeout=max(0.0, (deadline.target_at - timing.now()).total_seconds()))
            if not portal_task.done():
                portal_task.cancel()
    portal = [] if portal_task.cancelled() else portal_task.result()
    # Also reuse same-lane bids published before recovery or while another price source was still running.
    carried = [o for o in session.for_request(ctx, rfq) if o.source in (Source.PORTAL, Source.BID, Source.RATEDESK)]
    all_options = dict.fromkeys(sheets + portal + offers + pickups + carried)
    return combine(rfq, list(all_options), feedback.pickup_rule(ctx))
