"""From the customer's acceptance to the airline's confirmation (R22–R24). Every step is an event; mail leaves
through `core.outbox` only."""

import re
from datetime import UTC, datetime

import httpx

from pixel.agents.rfq import compose, extract
from pixel.api.client import ApiError
from pixel.api.portal import BookingUnavailable, PortalBooking, RateExpired
from pixel.contracts import Booking, BookingStatus, Message, Quote, QuoteStatus
from pixel.core import db, events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import calc, portal_rates

ACCEPT = re.compile(
    r"(?:^|[,;:])\s*(?:yes\s+)?(?:"
    r"(?:we|i)\s+(?:hereby\s+)?(?:accept|approve)(?:\s+(?:it|(?:this|your|the)\s+(?:offer|quote|quotation|rate)))?"
    r"|(?:we|i)\s+confirm\s+(?:the|this|our)\s+booking"
    r"|(?:please\s+)?go ahead(?:\s+and\s+book(?:\s+it)?|\s+with (?:this|the) (?:booking|shipment))?"
    r"|(?:please\s+)?book\s+(?:it|this(?:\s+shipment)?|the shipment)"
    r"|please\s+(?:book|proceed)(?:\s+with\s+(?:the|this|our)\s+(?:booking|shipment))?"
    r"|we['’]ll take (?:it|the offer)|let['’]s (?:book it|proceed)|(?:offer|quote) accepted|accepted"
    r"|bitte\s+buchen(?:\s+sie\s+(?:die|diese)\s+sendung)?|buchen\s+sie\s+bitte(?:\s+die sendung)?"
    r"|wir\s+(?:beauftragen\s+sie|nehmen\s+(?:ihr|das)\s+angebot\s+an)|einverstanden|agreed)"
    r"\s*(?:[,!.]|$)",
    re.IGNORECASE,
)
DECLINE = re.compile(
    r"\b(decline|no thanks|too (?:low|high)|not competitive|pass on|not interested|too expensive|went with|"
    r"another (?:offer|forwarder)|"
    r"(?:not|cannot|can['’]t|won['’]t|don['’]t|unable to)\s+"
    r"(?:yet\s+)?(?:go ahead|accept(?:ed)?|book(?:ed)?|proceed|confirm(?:ed)?|take it|move forward|be moving forward)|"
    r"nicht (?:buchen|annehmen|fortfahren)|leider nicht|cancel|absage|ablehnen|zu teuer|kein interesse)\b",
    re.IGNORECASE,
)
CONFIRMED = re.compile(r"booking\s+(\S+)\s+(?:is\s+)?confirmed", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def intent(text: str) -> str:
    """accept, decline or other, by rules. A decline wins over an accept word ("we cannot accept")."""
    if DECLINE.search(text):
        return "decline"
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", text):
        if sentence.rstrip().endswith("?") or re.search(
            r"\b(?:if|unless|whether|before|once|would|could|can you|should|not|yet|when|"
            r"falls|wenn|sobald|ob|bevor|nicht|können sie)\b",
            sentence,
            re.IGNORECASE,
        ):
            continue
        if ACCEPT.search(sentence):
            return "accept"
    return "other"


async def handle_reply(ctx: Context, message: Message) -> None:
    """The customer answered our quote: acceptance -> book; decline -> quote declined; else a person answers."""
    quote = quotes.for_session(ctx, message.session_id)
    own = extract.HISTORY.split(message.body, maxsplit=1)[0].strip()
    subject = "" if extract.REPLY_SUBJECT.match(message.subject) else message.subject
    said = intent(own or subject)
    if quote is not None and quote.sent_at is None and quote.status == QuoteStatus.DRAFT:
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer replied to an unsent draft", "intent": said, "quote_id": quote.id},
        )  # fmt: skip
        return
    if quote is None or said == "other":
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply needs a person", "intent": said, "has_quote": quote is not None},
        )  # fmt: skip
        return
    if said == "decline":
        quotes.set_status(ctx, quote.id, QuoteStatus.DECLINED)
        events.emit(ctx, "customer.declined", "crm", session_id=quote.session_id, message_id=message.id)
        return
    if quote.status == QuoteStatus.ACCEPTED:
        original = ctx.db.execute(
            "SELECT message_id FROM events WHERE session_id = ? AND kind = 'customer.accepted' ORDER BY id LIMIT 1",
            (quote.session_id,),
        ).fetchone()
        if original and original[0] == message.id:
            await book(ctx, quote.id, reply_to=message.id, resume=True)
        return
    with db.atomic(ctx.db):
        quotes.set_status(ctx, quote.id, QuoteStatus.ACCEPTED)
        events.emit(ctx, "customer.accepted", "crm", session_id=quote.session_id, message_id=message.id)
    await book(ctx, quote.id, reply_to=message.id)


async def book(ctx: Context, quote_id: str, reply_to: int | None = None, *, resume: bool = False) -> Booking:
    """Creates the shipment (its CRM id is the `team_reference`), books by `rate_id`; on 410 fetches a fresh
    rate and books that; on 503 or without a rate_id books by email with the carrier. Tells the customer."""
    quote = quotes.get(ctx, quote_id)
    if quote is None:
        raise KeyError(quote_id)
    existing = bookings.for_session(ctx, quote.session_id)
    if existing and existing.status in (BookingStatus.BOOKED, BookingStatus.CONFIRMED):
        if resume and existing.status == BookingStatus.BOOKED:
            await _notify_booked(ctx, quote, existing, reply_to)
        return existing
    if existing and (
        (not resume and existing.status == BookingStatus.REQUESTED)
        or (resume and existing.status == BookingStatus.FAILED)
    ):
        return existing
    if existing:
        booking = existing
    else:
        shipment = shipments.for_session(ctx, quote.session_id) or shipments.create(ctx, quote)
        booking = Booking(
            id=f"B-{shipment.id}", quote_id=quote.id, session_id=quote.session_id, team_reference=shipment.id,
            via="portal", carrier=quote.option.carrier, rate_id=quote.option.rate_id, price=calc.air_price(quote.option),
            currency=quote.currency, created_at=datetime.now(UTC),
        )  # fmt: skip
        _save(ctx, booking, "booking")
        events.emit(
            ctx, "booking.requested", "crm", session_id=quote.session_id,
            payload={"shipment": shipment.id, "rate_id": booking.rate_id, "carrier": booking.carrier},
        )  # fmt: skip

    if booking.via == "email" or not booking.rate_id:
        return await _book_by_mail(ctx, quote, booking, "booking by email")
    try:
        done = await _portal_book(ctx, booking.rate_id, booking.team_reference)
    except RateExpired:
        try:
            fresh = await portal_rates.refresh(ctx, quote.option)
            events.emit(
                ctx, "option.found", "crm", session_id=quote.session_id,
                payload={"why": "rate expired, fresh rate for booking", "option": fresh.model_dump(mode="json")},
            )  # fmt: skip
            booking = booking.model_copy(update={"rate_id": fresh.rate_id})
            _save(ctx, booking, "booking")
            done = await _portal_book(ctx, fresh.rate_id, booking.team_reference)
        except BookingUnavailable as e:
            return await _book_by_mail(ctx, quote, booking, e.hint)
        except (RateExpired, ApiError, httpx.HTTPError) as e:
            return _failed(ctx, booking, f"fresh rate: {e!r}")
    except BookingUnavailable as e:
        return await _book_by_mail(ctx, quote, booking, e.hint)
    except (ApiError, httpx.HTTPError) as e:  # 404 unknown rate_id, 409 booked under another reference
        return _failed(ctx, booking, repr(e))

    booking = booking.model_copy(
        update={
            "status": BookingStatus.BOOKED, "booking_reference": done.booking_reference, "rate_id": done.rate_id,
            "price": done.price, "currency": done.currency,
        }
    )  # fmt: skip
    _save(ctx, booking, "booked")
    events.emit(
        ctx, "booking.booked", "crm", session_id=quote.session_id,
        payload={"booking_reference": done.booking_reference, "shipment": booking.team_reference, "price": str(done.price),
                 "currency": done.currency, "airline": done.airline},
    )  # fmt: skip
    await _notify_booked(ctx, quote, booking, reply_to)
    return booking


async def _notify_booked(ctx: Context, quote: Quote, booking: Booking, reply_to: int | None) -> None:
    await _tell_customer(ctx, quote, reply_to, "booked", airline=booking.carrier,
                         price=compose.money(quote.total), currency=quote.currency,
                         reference=booking.team_reference, airline_reference=booking.booking_reference or "-")  # fmt: skip


async def handle_confirmation(ctx: Context, message: Message) -> None:
    """ "Booking <reference> confirmed" from the airline -> booking confirmed, shipment updated, customer told."""
    match = CONFIRMED.search(message.subject) or CONFIRMED.search(message.body)
    reference = match.group(1).strip(".,:;") if match else None
    booking = (reference and bookings.by_reference(ctx, reference)) or bookings.for_session(ctx, message.session_id)
    if booking is None:
        events.emit(
            ctx, "error", "crm", session_id=message.session_id, message_id=message.id,
            payload={"error": "confirmation for an unknown booking", "booking_reference": reference},
        )  # fmt: skip
        return
    if booking.status != BookingStatus.CONFIRMED:
        booking = booking.model_copy(
            update={
                "status": BookingStatus.CONFIRMED, "confirmed_at": message.sent_at,
                "booking_reference": booking.booking_reference or reference,
            }
        )  # fmt: skip
        _save(ctx, booking, "confirmed")
        events.emit(
            ctx, "booking.confirmed", "crm", session_id=booking.session_id, message_id=message.id,
            payload={"booking_reference": booking.booking_reference, "shipment": booking.team_reference},
        )  # fmt: skip
    quote = quotes.get(ctx, booking.quote_id)
    if quote:
        await _tell_customer(ctx, quote, None, "confirmed", airline=booking.carrier,
                             airline_reference=booking.booking_reference or "-", reference=booking.team_reference)  # fmt: skip


async def _portal_book(ctx: Context, rate_id: str, team_reference: str) -> PortalBooking:
    """One retry on a lost connection: the portal answers a repeated booking with 200 and the same booking. A dry
    run never books: only the live service may, and a dry process meets real acceptances too (a replay, a local
    run on the real mailbox). It falls through to the booking mail, which the outbox holds."""
    if ctx.settings.dry_run:
        raise BookingUnavailable("dry run: no portal booking")
    try:
        return await ctx.portal.book(rate_id, team_reference)
    except httpx.TransportError:
        return await ctx.portal.book(rate_id, team_reference)


async def _book_by_mail(ctx: Context, quote: Quote, booking: Booking, why: str) -> Booking:
    address = _airline_address(ctx, booking.carrier, why)
    if address is None:
        return _failed(ctx, booking, f"no address to book by mail: {why}")
    booking = booking.model_copy(update={"via": "email"})
    _save(ctx, booking, "booking")
    lines = [
        "Hello,",
        "",
        f"please book the following air freight shipment for us (our reference {booking.team_reference}):",
        "",
        f"Lane: {_lane(quote)}",
        f"Weight: {quote.rfq.weight_kg} kg" + (f", volume {quote.rfq.volume_cbm} cbm" if quote.rfq.volume_cbm else ""),
        f"Price: {booking.currency} {booking.price}",
    ]
    if booking.rate_id:
        lines.append(f"Rate id: {booking.rate_id}")
    lines += ["", "Please confirm with your booking reference.", "", "Thank you,", "PIXEL Air Freight"]
    try:
        await outbox.send(
            ctx, to=address, subject=f"Booking request {booking.team_reference}: {_lane(quote)}",
            body="\n".join(lines), session_id=quote.session_id, purpose="booking",
        )  # fmt: skip
    except outbox.NotSent as e:
        return _failed(ctx, booking, f"booking mail not sent: {e.reason}")
    events.emit(
        ctx, "booking.requested", "crm", session_id=quote.session_id,
        payload={"via": "email", "to": address, "why": why, "shipment": booking.team_reference},
    )  # fmt: skip
    return booking


def _airline_address(ctx: Context, carrier: str, hint: str) -> str | None:
    found = EMAIL.search(hint or "")
    if found:
        return found.group(0).rstrip(".")
    row = ctx.db.execute("SELECT email FROM carriers WHERE LOWER(name) = LOWER(?)", (carrier,)).fetchone()
    return row["email"] if row else None


def _failed(ctx: Context, booking: Booking, why: str) -> Booking:
    booking = booking.model_copy(update={"status": BookingStatus.FAILED})
    _save(ctx, booking, "failed")
    events.emit(
        ctx, "booking.failed", "crm", session_id=booking.session_id,
        payload={"why": why, "rate_id": booking.rate_id, "shipment": booking.team_reference},
    )  # fmt: skip
    return booking


def _save(ctx: Context, booking: Booking, status: str) -> None:
    bookings.save(ctx, booking)
    shipment = shipments.get(ctx, booking.team_reference)
    if shipment:
        shipments.save(
            ctx,
            shipment.model_copy(
                update={
                    "booking_id": booking.id, "booking_reference": booking.booking_reference,
                    "carrier": booking.carrier, "status": status, "updated_at": datetime.now(UTC),
                }
            ),
        )  # fmt: skip


CUSTOMER = {
    "en": {
        "hello_name": "Hello {name},", "hello": "Hello,",
        "booked": "Thanks for the go-ahead. Your shipment from {origin} to {destination} ({weight} kg) is now booked "
                  "with {airline} at {price} {currency} {scope}.\n\nOur reference for you is {reference}, the "
                  "airline's is {airline_reference}. I'll let you know as soon as {airline} confirms.",
        "confirmed": "Good news: {airline} has confirmed your booking {airline_reference} from {origin} to "
                     "{destination} ({weight} kg).\n\nYour reference with us stays {reference}. If you'd like "
                     "to know where your shipment is, just ask.",
        "bye": "Best regards,\n{signer}\n" + compose.COMPANY,
    },
    "de": {
        "hello_name": "Hallo {name},", "hello": "Guten Tag,",
        "booked": "Danke für Ihre Zusage. Ihre Sendung von {origin} nach {destination} ({weight} kg) ist jetzt bei "
                  "{airline} gebucht, zu {price} {currency} {scope}.\n\nUnsere Referenz für Sie ist {reference}, "
                  "die der Airline {airline_reference}. Ich melde mich, sobald {airline} bestätigt.",
        "confirmed": "Gute Nachricht: {airline} hat Ihre Buchung {airline_reference} von {origin} nach "
                     "{destination} ({weight} kg) bestätigt.\n\nIhre Referenz bei uns bleibt {reference}. Wenn "
                     "Sie wissen möchten, wo Ihre Sendung gerade ist, fragen Sie einfach.",
        "bye": "Viele Grüße\n{signer}\n" + compose.COMPANY,
    },
}  # fmt: skip


def _lang(quote: Quote) -> str:
    return compose.language_of(quote.rfq)


async def _tell_customer(ctx: Context, quote: Quote, reply_to: int | None, what: str, **facts: str) -> None:
    """A short mail in the customer's language, in the quote's thread, signed by the same person (R24, R31).
    Numbers are formatted by code; no model call, so the quote's spend stays one call."""
    delivered = f"booking-notice:{quote.id}:{what}"
    if db.get(ctx.db, delivered):
        return
    lang = _lang(quote)
    t = CUSTOMER[lang]
    prose = compose.stored(ctx, quote.session_id)
    name = prose.first_name if prose else None
    scope = "all-in"
    if quote.rfq.door_pickup and not quote.option.trucker:
        scope = (
            "für den Flughafen-zu-Flughafen-Transport ohne Abholung"
            if lang == "de"
            else "for airport-to-airport transport, excluding pickup"
        )
    text = t[what].format(scope=scope, origin=quote.option.origin, destination=quote.option.destination,
                          weight=compose.weight(quote.rfq.weight_kg, lang), **facts)  # fmt: skip
    body = "\n\n".join([t["hello_name"].format(name=name) if name else t["hello"], text,
                          t["bye"].format(signer=ctx.settings.signer)])  # fmt: skip
    subject = compose.reply_subject(compose.customer_subject(ctx, quote.rfq.message_id)) or quote.subject
    try:
        await outbox.send(
            ctx, to=quote.rfq.customer, subject=subject, body=body,
            session_id=quote.session_id, in_reply_to=reply_to or quote.sent_message_id or quote.rfq.message_id,
            purpose="booking",
        )  # fmt: skip
    except outbox.NotSent:
        return  # outbox emitted mail.held; the draft waits in the sessions UI
    db.put(ctx.db, delivered, "sent")


def _lane(quote: Quote) -> str:
    return f"{quote.option.origin} → {quote.option.destination}"
