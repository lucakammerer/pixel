import asyncio
from datetime import timedelta
from decimal import Decimal

from pixel.api.mailbox import RATEDESK
from pixel.contracts import LaneTariff, RateOption, Rfq, Source
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import availability, bids, calc, pickup, portal_rates, selection, session, store

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


def _priced(ctx: Context, rfq: Rfq, tariffs: list[LaneTariff], emit: bool = True) -> list[RateOption]:
    found = []
    suspended = availability.suspended_carriers(ctx, rfq)
    for tariff in tariffs:
        if tariff.carrier.casefold() in suspended:
            continue
        try:
            fx = None if tariff.currency == "EUR" else _fx_eur(ctx, tariff)
            option = calc.price_tariff(tariff, rfq, fx)
        except ValueError as exc:
            events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                        payload={"source": "sheet", "carrier": tariff.carrier, "error": str(exc)})  # fmt: skip
            continue
        if emit:
            _found(ctx, rfq, option)
        found.append(option)
    return found


def sheet_options(ctx: Context, rfq: Rfq) -> list[RateOption]:
    return _priced(ctx, rfq, store.lookup(ctx, rfq.origin, rfq.destination, calc.ready_day(rfq)))


def expired_sheet_options(ctx: Context, rfq: Rfq) -> list[RateOption]:
    """The last resort for a cargo ready after its sheets run out (next case 5): today's sheet of each carrier that
    has none valid on the ready day, priced but not logged. A price beats no answer (R1), so `options` and
    `agents/rfq/agent.py` `fallback` take these only when nothing else has a price; logged earlier, they would count
    their airlines as priced and keep the bids from being asked."""
    day, today = calc.ready_day(rfq), rfq.received_at.date()
    if day <= today:
        return []
    valid = {t.carrier.casefold() for t in store.lookup(ctx, rfq.origin, rfq.destination, day)}
    tariffs = [t for t in store.lookup(ctx, rfq.origin, rfq.destination, today) if t.carrier.casefold() not in valid]
    return _priced(ctx, rfq, tariffs, emit=False)


def _portal_down(ctx: Context, rfq: Rfq) -> bool:
    return (
        ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='option.failed' AND json_extract(payload,'$.source')='portal'"
            " AND json_extract(payload,'$.error')='RatesUnavailable'",
            (rfq.session_id,),
        ).fetchone()
        is not None
    )


def _ask_desk(ctx: Context, rfq: Rfq) -> bool:
    """The operator may ask the desk after a 503, once per lane."""
    return _portal_down(ctx, rfq) and _desk_enquiry(ctx, rfq) is None


def _desk_enquiry(ctx: Context, rfq: Rfq) -> int | None:
    asked = ctx.db.execute(
        "SELECT message_id FROM events WHERE session_id=? AND kind='bid.requested' AND json_extract(payload,'$.email')=?"
        " AND json_extract(payload,'$.origin')=? AND json_extract(payload,'$.destination')=?",
        (rfq.session_id, RATEDESK, rfq.origin, rfq.destination),
    ).fetchone()
    return asked[0] if asked else None


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
    if ctx is not None:
        suspended = availability.suspended_carriers(ctx, rfq)
        matching = [option for option in matching if option.trucker or option.carrier.casefold() not in suspended]
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


async def _reread(ctx: Context, rfq: Rfq, deadline: Deadline, now: asyncio.Event) -> None:
    """`portal_rates.reread`; its failure must not cancel the other price sources of the TaskGroup."""
    try:
        await portal_rates.reread(ctx, rfq, deadline, now)
    except Exception as e:  # noqa: BLE001
        events.emit(ctx, "error", "pricing", session_id=rfq.session_id,
                    payload={"where": "portal_reread", "error": type(e).__name__})  # fmt: skip


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Every priced option for the request: sheets (instant), portal and bids in parallel, pickup added for
    door-to-port. Emits `option.found` per option as it arrives, so the fallback always has the best so far."""
    sheets = sheet_options(ctx, rfq)
    suspended = availability.suspended_carriers(ctx, rfq)
    carriers = [
        (name, email) for name, email in store.carriers(ctx, "airline", rfq.destination)
        if selection.requested_carrier(rfq, name) and name.casefold() not in suspended
    ]  # fmt: skip
    priced = {o.carrier.casefold() for o in sheets if selection.requested_carrier(rfq, o.carrier, o.carrier_code)}

    def priced_airlines() -> set[str]:
        return {
            option.carrier.casefold() for option in session.for_request(ctx, rfq)
            if not option.trucker and selection.requested_carrier(rfq, option.carrier, option.carrier_code)
            and option.carrier.casefold() not in suspended
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
        down = _portal_down(ctx, rfq)
        desk_id = _desk_enquiry(ctx, rfq)
        # On a 503 the desk supplies portal rates. On a lane with no other source it may name off-portal airlines.
        if desk_id is None and (down or not (priced or unpriced)):
            unpriced.append(("Rate desk", RATEDESK))
        mailed = await bids.request(ctx, rfq, unpriced) if unpriced else []
        reread_now = asyncio.Event()
        reread = group.create_task(_reread(ctx, rfq, deadline, reread_now)) if down else None
        pending_desk = (
            (desk_id or _desk_enquiry(ctx, rfq)) if down and selection.requested_options(rfq, sheets) else None
        )
        if pending_desk is not None:
            if pending_desk not in mailed:
                mailed.append(pending_desk)
            offers = await bids.collect(ctx, rfq.session_id, deadline, mailed, pending_desk=pending_desk)
        elif mailed:
            offers = await bids.collect(ctx, rfq.session_id, deadline, mailed)
        if reread is not None:
            reread_now.set()
            by = min(deadline.received_at + timedelta(seconds=bids.DESK_CUTOFF_S), deadline.hard_at)
            await asyncio.wait({reread}, timeout=max(0.0, (by - timing.now()).total_seconds()))
            reread.cancel()
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
    found = combine(rfq, list(all_options), feedback.pickup_rule(ctx), ctx)
    if found or not (expired := expired_sheet_options(ctx, rfq)):
        return found
    for option in expired:
        _found(ctx, rfq, option)
    return combine(rfq, list(dict.fromkeys(expired + pickups + carried)), feedback.pickup_rule(ctx), ctx)
