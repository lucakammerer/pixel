"""Source a first quote from the session page using the live pricing and outbox paths."""

import asyncio
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from pixel.agents.rfq import agent, compose
from pixel.api.mailbox import RATEDESK
from pixel.contracts import Event, MailKind, Preference, Rfq
from pixel.core import events, feedback, outbox, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline, deadline_for
from pixel.crm import quotes
from pixel.pricing import bids, engine, pickup, portal_rates, selection, session, store

SOURCE_BUDGET_S = 20


def request_facts(ctx: Context, sid: int) -> Rfq | None:
    try:
        return session.rfq(ctx, sid)
    except ValidationError:
        return None  # older extraction events can contain a summary rather than an RFQ


def usable(rfq: Rfq | None) -> bool:
    return bool(
        rfq and rfq.origin and rfq.destination and rfq.weight_kg > 0 and not agent.REQUIRED.intersection(rfq.missing)
    )


def can_record(evs: list[Event]) -> bool:
    kinds = {event.kind for event in evs}
    return "quote.waiting" in kinds and not kinds.intersection({"option.found", "bid.requested"})


def _record(ctx: Context, sid: int, rfq: Rfq | None, mail, deadline: Deadline, form: dict[str, str]) -> str:
    if usable(rfq) or not can_record(events.for_session(ctx, sid)):
        raise ValueError(
            "Shipment details can be entered after the automatic read stops, before any rates are sourced."
        )
    postcode = form.get("pickup_postcode", "").strip()
    door = form.get("door_pickup") == "yes"
    if door and not re.fullmatch(r"[0-9]{5}", postcode):
        raise ValueError("Enter the five-digit pickup postcode from the request.")
    zone = pickup.zone_of(postcode) if door else None
    origin = form.get("origin", "").strip().upper() or (store.airport_of(ctx, zone) if zone else "")
    destination = form.get("destination", "").strip().upper()
    if not re.fullmatch("[A-Z]{3}", origin or "") or not re.fullmatch("[A-Z]{3}", destination) or origin == destination:
        raise ValueError("Enter the request’s origin and destination as distinct three-letter airport codes.")
    try:
        weight = Decimal(form.get("weight_kg", ""))
        volume = Decimal(form.get("volume_cbm", "") or "0")
        pieces = int(form["pieces"]) if form.get("pieces") else None
        if not (
            weight.is_finite() and weight > 0 and volume.is_finite() and volume >= 0 and (pieces is None or pieces > 0)
        ):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError(
            "Enter a positive gross weight, a nonnegative volume, and a positive piece count if known."
        ) from None
    currency = form.get("currency", "").strip().upper()
    language = form.get("language", "").strip().lower()
    if (currency and not re.fullmatch("[A-Z]{3}", currency)) or not re.fullmatch("[a-z]{2,3}", language):
        raise ValueError("Use a three-letter currency code if requested and a two- or three-letter language code.")
    try:
        preference = Preference(form.get("preference"))
    except ValueError:
        raise ValueError("Choose cheapest or fastest from the customer's request.") from None
    try:
        ready = date.fromisoformat(form["ready_on"]) if form.get("ready_on") else None
        arrival = date.fromisoformat(form["arrive_by"]) if form.get("arrive_by") else None
        transit = int(form["max_transit_days"]) if form.get("max_transit_days") else None
        if (ready and arrival and ready > arrival) or (transit is not None and transit <= 0):
            raise ValueError
    except ValueError:
        raise ValueError("Enter valid shipment dates and a positive transit limit in days.") from None
    recorded = Rfq(
        session_id=sid, message_id=sid, customer=mail["sender"], origin=origin, destination=destination, weight_kg=weight,
        volume_cbm=volume, pieces=pieces, preference=preference, door_pickup=door,
        pickup_postcode=postcode if door else None, pickup_zone=zone, currency=currency or None, language=language,
        received_at=deadline.received_at, deadline_at=deadline.expires_at, extracted_by="operator",
        excluded_carriers=tuple(name.strip() for name in form.get("excluded_carriers", "").split(",") if name.strip()),
        required_carriers=tuple(name.strip() for name in form.get("required_carriers", "").split(",") if name.strip()),
        ready_on=ready, arrive_by=arrival, max_transit_days=transit,
        missing=tuple(key for key in rfq.missing if key not in agent.REQUIRED | {"pickup"}) if rfq else (),
    )  # fmt: skip
    events.emit(ctx, "rfq.extracted", "human", session_id=sid, message_id=sid, payload=recorded.model_dump(mode="json"))
    return "Shipment details saved. Find sheet and portal rates next."


async def _request_bids(ctx: Context, rfq: Rfq, *, desk: bool = False) -> str:
    if desk:
        if not engine._ask_desk(ctx, rfq):
            raise ValueError("Ask the rate desk after the portal returns 503, once for this lane.")
        carriers, truckers = [("Rate desk", RATEDESK)], []
    else:
        priced = {option.carrier.casefold() for option in session.for_request(ctx, rfq) if not option.trucker}
        carriers = bids.worth_asking(ctx, rfq, store.carriers(ctx, "airline", rfq.destination))
        carriers = [
            (name, email) for name, email in carriers
            if name.casefold() not in priced and selection.requested_carrier(rfq, name)
        ]  # fmt: skip
        truckers = store.carriers(ctx, "trucker", rfq.pickup_zone) if rfq.door_pickup and rfq.pickup_zone else []
    asked = {
        event.payload.get("email")
        for event in events.for_session(ctx, rfq.session_id)
        if event.kind == "bid.requested"
        and (event.payload.get("origin"), event.payload.get("destination")) == (rfq.origin, rfq.destination)
    }
    carriers = [(name, email) for name, email in carriers if email not in asked]
    truckers = [(name, email) for name, email in truckers if email not in asked]
    if not (carriers or truckers):
        return "No new eligible carriers to contact. Check Activity for earlier requests and replies."
    air, road = await asyncio.gather(bids.request(ctx, rfq, carriers), bids.request(ctx, rfq, truckers, pickup=True))
    count = len(air) + len(road)
    if not count:
        raise ValueError("No bid emails were sent. Check Activity for held mail or errors.")
    return f"Bid requests sent to {count} contacts. Replies appear in Activity and Options."


async def source(ctx: Context, sid: int, action: str, form: dict[str, str]) -> str:
    """An explicit operator action on an open, unquoted customer request."""
    row = ctx.db.execute(
        "SELECT m.sender, m.kind, s.received_at, s.deadline_at, s.quote_message_id, s.mode"
        " FROM sessions s JOIN messages m ON m.id=s.id AND m.direction='in' WHERE s.id=?",
        (sid,),
    ).fetchone()
    if not row or row["kind"] != MailKind.CUSTOMER_RFQ or row["mode"] != "human":
        raise ValueError("Take over an open customer request before sourcing rates.")
    deadline = deadline_for(datetime.fromisoformat(row["received_at"]), ctx.settings)
    expires = datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else deadline.expires_at
    remaining = (expires - timing.now()).total_seconds()
    quote = quotes.for_session(ctx, sid)
    if remaining <= 0 or row["quote_message_id"] or (quote and quote.sent_at) or outbox.quoted(ctx, sid):
        raise ValueError("This request has expired or a quote is already being handled. Check Activity.")
    rfq = request_facts(ctx, sid)
    if action == "record":
        return _record(ctx, sid, rfq, row, deadline, form)
    if not usable(rfq):
        raise ValueError("Confirm the shipment details before sourcing or drafting a quote.")
    if action == "draft":
        now = timing.now()
        fresh = [o for o in session.for_request(ctx, rfq) if not o.valid_until or o.valid_until > now]
        options = engine.combine(rfq, fresh, feedback.pickup_rule(ctx), ctx)
        compliant = selection.compliant_options(rfq, selection.allowed(rfq, options))
        if rfq.door_pickup:
            compliant = [option for option in compliant if option.trucker]
        if not compliant:
            raise ValueError("No complete price yet. Check airline, transit and pickup requirements.")
        agent._quote(ctx, rfq, compliant, compose.stored(ctx, sid), fallback=False)
        return "Priced draft ready. Review the scope and wording before sending."
    async with asyncio.timeout(min(remaining, SOURCE_BUDGET_S)):
        if action == "portal":
            sheets = engine.sheet_options(ctx, rfq)
            if ctx.portal is None:
                return f"Found {len(sheets)} sheet rates. This preview has no live portal connection."
            portal = await portal_rates.options(ctx, rfq, deadline)
            return f"Found {len(sheets)} sheet and {len(portal)} portal rates. Check Options and Activity."
        if action in {"bids", "desk"}:
            return await _request_bids(ctx, rfq, desk=action == "desk")
    raise ValueError("Choose a sourcing action.")
