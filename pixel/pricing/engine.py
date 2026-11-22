import asyncio
from decimal import Decimal

from pixel.api.mailbox import RATEDESK
from pixel.contracts import LaneTariff, RateOption, Rfq, Source
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, pickup, portal_rates, selection, session, store

PORTAL_HEAD_START_S = 1.0


def _found(ctx: Context, rfq: Rfq, option: RateOption) -> None:
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip


def _fx_eur(ctx: Context, tariff: LaneTariff) -> Decimal | None:
    """EUR per unit of a sheet's currency: as the sheet or its tariff mail states it (`tariff.ingested`), else the
    newest the portal quoted for that currency. Never a constant: without either the sheet is not priced."""
    stated = (
        ctx.db.execute(
            "SELECT json_extract(payload,'$.fx_eur') FROM events WHERE kind='tariff.ingested'"
            " AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.fx_eur') IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (tariff.source,),
        ).fetchone()
        or ctx.db.execute(
            "SELECT json_extract(payload,'$.option.fx_eur') FROM events WHERE kind='option.found'"
            " AND json_extract(payload,'$.option.source')=? AND json_extract(payload,'$.option.currency')=?"
            " AND json_extract(payload,'$.option.fx_eur') IS NOT NULL ORDER BY id DESC LIMIT 1",
            (Source.PORTAL.value, tariff.currency),
        ).fetchone()
    )
    return Decimal(str(stated[0])) if stated else None


def sheet_options(ctx: Context, rfq: Rfq) -> list[RateOption]:
    found = []
    for tariff in store.lookup(ctx, rfq.origin, rfq.destination, rfq.received_at.date()):
        try:
            fx = None if tariff.currency == "EUR" else _fx_eur(ctx, tariff)
            option = calc.price_tariff(tariff, rfq, fx)
        except ValueError as exc:
            events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                        payload={"source": "sheet", "carrier": tariff.carrier, "error": str(exc)})  # fmt: skip
            continue
        _found(ctx, rfq, option)
        found.append(option)
    return found


def _ask_desk(ctx: Context, rfq: Rfq, alone: bool = False) -> bool:
    """R21: a rate read for this request answered 503, so the rate API is down; the desk mails the portal's rate per
    airline with its rate id. A 404 or 429 leaves the API up: the desk would only repeat it. `alone`: no sheet, no
    portal rate and no airline to mail, so nothing else can price the lane; the desk names the airlines that fly it
    off the portal (R1). Once per request."""
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
    return (bool(down) or alone) and not asked


def _distrusted(ctx: Context, rfq: Rfq, option: RateOption) -> None:
    """Once per bid: an `option.failed` that `feedback.attention` shows, so the reading can be repaired."""
    name = option.trucker or option.carrier
    seen = ctx.db.execute(
        "SELECT 1 FROM events WHERE session_id=? AND kind='option.failed' AND json_extract(payload,'$.error')="
        "'implausible' AND json_extract(payload,'$.carrier')=? AND json_extract(payload,'$.total_eur')=?",
        (rfq.session_id, name, str(option.total_eur)),
    ).fetchone()
    if not seen:
        events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                    message_id=option.evidence_message_id,
                    payload={"source": "pickup" if option.trucker else "bid", "carrier": name,
                             "total_eur": str(option.total_eur), "error": "implausible"})  # fmt: skip


def combine(
    rfq: Rfq, options: list[RateOption], rule: str = selection.DEFAULT_RULE, ctx: Context | None = None
) -> list[RateOption]:
    """Airline options, each with the balanced pickup added for door-to-port (Q4: the best price includes it).
    Without a pickup price a door-to-port request is priced port-to-port rather than not at all. A bid
    `selection.implausible` distrusts takes no part; with `ctx` it is recorded."""
    matching = [o for o in options if session.on_lane(o, rfq)]
    peers = selection.allowed(rfq, matching) or matching  # an airline the customer ruled out is no yardstick
    distrusted = [o for o in matching if selection.implausible(o, peers)]
    for option in distrusted if ctx is not None else ():
        _distrusted(ctx, rfq, option)
    matching = [o for o in matching if o not in distrusted]
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
    carriers = [
        (name, email) for name, email in store.carriers(ctx, "airline", rfq.destination)
        if selection.requested_carrier(rfq, name)
    ]  # fmt: skip
    priced = {o.carrier.casefold() for o in sheets if selection.requested_carrier(rfq, o.carrier, o.carrier_code)}

    def priced_airlines() -> set[str]:
        return {
            option.carrier.casefold() for option in session.for_request(ctx, rfq)
            if not option.trucker and selection.requested_carrier(rfq, option.carrier, option.carrier_code)
        }  # fmt: skip

    candidates = bids.worth_asking(ctx, rfq, [(n, e) for n, e in carriers if n.casefold() not in priced])
    offers = []
    async with asyncio.TaskGroup() as group:
        pickup_task = group.create_task(pickup.options(ctx, rfq, deadline))
        portal_task = group.create_task(portal_rates.options(ctx, rfq, deadline))
        # Fast portal prices save unnecessary mails. An outage or a slow rate read must still
        # leave mail-only airlines time to answer, including after a late restart.
        head_start = min(PORTAL_HEAD_START_S, max(0.0, (deadline.target_at - timing.now()).total_seconds()))
        await asyncio.wait({portal_task}, timeout=head_start)
        priced.update(priced_airlines())
        unpriced = [(name, email) for name, email in candidates if name.casefold() not in priced]
        if not (priced or unpriced):  # nothing to mail: the portal's own answer decides whether the desk is needed
            await asyncio.wait({portal_task}, timeout=max(0.0, (deadline.target_at - timing.now()).total_seconds()))
            priced.update(priced_airlines())
        if _ask_desk(ctx, rfq, alone=not (priced or unpriced)):
            unpriced.append(("Rate desk", RATEDESK))
        if unpriced:
            mailed = await bids.request(ctx, rfq, unpriced)
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
    return combine(rfq, list(all_options), feedback.pickup_rule(ctx), ctx)
