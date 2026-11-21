"""From the customer's acceptance to the airline's confirmation (R22–R24). Every step is an event; mail leaves
through `core.outbox` only."""

import re
from datetime import UTC, datetime

import httpx

from pixel.agents.rfq import compose
from pixel.api.client import ApiError
from pixel.api.portal import BookingUnavailable, PortalBooking, RateExpired
from pixel.contracts import Booking, BookingStatus, Message, Quote, QuoteStatus
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments

ACCEPT = re.compile(
    r"\b(accept|accepted|book it|please book|go ahead|proceed|confirm|agreed|let'?s do it|we'?ll take it|"
    r"buchen|einverstanden|annehmen|nehmen wir|auftrag)\b",
    re.IGNORECASE,
)
DECLINE = re.compile(
    r"\b(decline|no thanks|not proceed|not interested|too expensive|went with|another (?:offer|forwarder)|"
    r"cancel|absage|ablehnen|zu teuer|kein interesse)\b",
    re.IGNORECASE,
)
CONFIRMED = re.compile(r"booking\s+(\S+)\s+(?:is\s+)?confirmed", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def intent(text: str) -> str:
    """accept, decline or other, by rules. A decline wins over an accept word ("we cannot accept")."""
    if DECLINE.search(text) or re.search(
        r"\b(?:not|cannot|can't|won't)\s+(?:accept|book|proceed)", text, re.IGNORECASE
    ):
        return "decline"
    return "accept" if ACCEPT.search(text) else "other"


async def handle_reply(ctx: Context, message: Message) -> None:
    """The customer answered our quote: acceptance -> book; decline -> quote declined; else a person answers."""
    quote = quotes.for_session(ctx, message.session_id)
    said = intent(f"{message.subject}\n{_new_text(message.body)}")
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
        return  # a second "yes" in the same thread books nothing twice
    quotes.set_status(ctx, quote.id, QuoteStatus.ACCEPTED)
    events.emit(ctx, "customer.accepted", "crm", session_id=quote.session_id, message_id=message.id)
    await book(ctx, quote.id, reply_to=message.id)


async def book(ctx: Context, quote_id: str, reply_to: int | None = None) -> Booking:
    """Creates the shipment (its CRM id is the `team_reference`), books by `rate_id`; on 410 fetches a fresh
    rate and books that; on 503 or without a rate_id books by email with the carrier. Tells the customer."""
    quote = quotes.get(ctx, quote_id)
    if quote is None:
        raise KeyError(quote_id)
    existing = bookings.for_session(ctx, quote.session_id)
    if existing and existing.status != BookingStatus.FAILED:
        return existing
    shipment = shipments.create(ctx, quote)
    booking = Booking(
        id=f"B-{shipment.id}", quote_id=quote.id, session_id=quote.session_id, team_reference=shipment.id,
        via="portal", carrier=quote.option.carrier, rate_id=quote.option.rate_id, price=quote.total,
        currency=quote.currency, created_at=datetime.now(UTC),
    )  # fmt: skip
    _save(ctx, booking, "booking")
    events.emit(
        ctx, "booking.requested", "crm", session_id=quote.session_id,
        payload={"shipment": shipment.id, "rate_id": booking.rate_id, "carrier": booking.carrier},
    )  # fmt: skip

    if not booking.rate_id:
        return await _book_by_mail(ctx, quote, booking, "the quote has no portal rate")
    try:
        done = await _portal_book(ctx, booking.rate_id, shipment.id)
    except RateExpired:
        try:
            fresh = await ctx.portal.rate(
                quote.option.origin, quote.option.destination, quote.option.carrier_code or quote.option.carrier,
                quote.rfq.weight_kg, quote.rfq.volume_cbm,
            )  # fmt: skip
            events.emit(
                ctx, "option.found", "crm", session_id=quote.session_id,
                payload={"why": "rate expired, fresh rate for booking", "option": fresh.model_dump(mode="json")},
            )  # fmt: skip
            booking = booking.model_copy(update={"rate_id": fresh.rate_id})
            _save(ctx, booking, "booking")
            done = await _portal_book(ctx, fresh.rate_id, shipment.id)
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
        payload={"booking_reference": done.booking_reference, "shipment": shipment.id, "price": str(done.price),
                 "currency": done.currency, "airline": done.airline},
    )  # fmt: skip
    await _tell_customer(ctx, quote, reply_to, "booked", airline=done.airline,
                         price=compose.money(done.price), currency=done.currency,
                         reference=shipment.id, airline_reference=done.booking_reference or "-")  # fmt: skip
    return booking


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
    if booking.status == BookingStatus.CONFIRMED:
        return
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
                  "with {airline} at {price} {currency} all-in.\n\nOur reference for you is {reference}, the "
                  "airline's is {airline_reference}. I'll let you know as soon as {airline} confirms.",
        "confirmed": "Good news: {airline} has confirmed your booking {airline_reference} from {origin} to "
                     "{destination} ({weight} kg).\n\nYour reference with us stays {reference}. If you'd like "
                     "to know where your shipment is, just ask.",
        "bye": "Best regards,\n{signer}\n" + compose.COMPANY,
    },
    "de": {
        "hello_name": "Hallo {name},", "hello": "Guten Tag,",
        "booked": "Danke für Ihre Zusage. Ihre Sendung von {origin} nach {destination} ({weight} kg) ist jetzt bei "
                  "{airline} gebucht, zu {price} {currency} all-in.\n\nUnsere Referenz für Sie ist {reference}, "
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
    lang = _lang(quote)
    t = CUSTOMER[lang]
    prose = compose.stored(ctx, quote.session_id)
    name = prose.first_name if prose else None
    text = t[what].format(origin=quote.option.origin, destination=quote.option.destination,
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
        pass  # outbox already emitted mail.held; the draft waits in the sessions UI


def _lane(quote: Quote) -> str:
    return f"{quote.option.origin} → {quote.option.destination}"


def _new_text(body: str) -> str:
    """The customer's own words: drop the quoted mail below."""
    kept = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^(On .+ wrote:|Am .+ schrieb|-----Original Message|From: )", line):
            break
        kept.append(line)
    return "\n".join(kept)
