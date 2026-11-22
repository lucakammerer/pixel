"""From the customer's acceptance to the airline's confirmation (R22–R24). Every step is an event; mail leaves
through `core.outbox` only."""

import asyncio
import re
from datetime import UTC, datetime

import httpx

from pixel.agents.rfq import compose, extract, followup
from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.api.portal import BookingUnavailable, NotOnPortal, PortalBooking, RateExpired, RateLimited
from pixel.contracts import Booking, BookingStatus, Message, Quote, QuoteStatus, Rfq
from pixel.core import db, events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import availability, calc, portal_rates, selection, session

NO_PRICE_ACK = re.compile(
    r"(?:understood[,.]?\s+)?(?:thanks|thank you)(?: for (?:letting me know|the update))?[.!]?",
    re.IGNORECASE,
)

ACCEPT = re.compile(
    r"\b(?:(?:we|i)\s+(?:hereby\s+)?(?:accept|approve)(?!\s+(?:that|no|any)\b)|accept(?:ed)?\s+(?:it|(?:this|your|the)\s+(?:offer|quote|quotation|rate))"
    r"|(?:offer|quote) accepted|confirm\s+(?:the|this|our)\s+booking|go ahead|book\s+(?:it|this|the\s+shipment|with)\b"
    r"|please\s+(?:book|proceed)(?=\s*(?:[.,!;]|$|\s+(?:it|this|the|with|now|as|and|our|right)\b))|let['’]s (?:book|proceed|do it)|we['’]ll take (?:it|the offer)|(?<!as )agreed"
    r"|(?:would|['’]d)\s+like\s+to\s+(?:book|proceed)|arrange\s+the\s+booking|place\s+the\s+(?:shipment|order)\s+with\s+you"
    r"|bitte\s+(?:\w+\s+){0,2}buchen|buchen\s+sie|gerne\s+buchen|sie\s+können\s+buchen|auftrag\b|einverstanden"
    r"|nehmen\s+(?:ihr|das)\s+angebot\s+an|angebot\s+angenommen|bitte\s+fortfahren|buchung\s+vornehmen"
    # "go with" books only our offer: "go with it/this/your quote" or "the rate you quoted", never "the airline we used"
    r"|(?:let['’]s|we['’]ll|we\s+will)\s+go\s+with\s+(?:(?:it|this|that|your)\b|the\b[^.\n]{0,40}?\byou\s+(?:quoted|offered|proposed)\b)"
    # ... unless the clause puts it off or names someone else: "instead", "not now", "next month", "from DHL"
    r"(?![^.!?;\n]*\b(?:instead|not\s+now|later|next\s+(?:week|month|time)|from\s+\w))"
    r"|merci\s+de\s+r[ée]server|veuillez\s+r[ée]server|nous\s+acceptons|r[ée]servez)",
    re.IGNORECASE,
)
DECLINE = re.compile(
    r"\b(decline|no thanks|(?<!not )too (?:low|high)|not competitive|pass on (?:this|it|that|your offer)|not interested|"
    r"(?<!not )too expensive|went with|"
    r"another (?:offer|forwarder)(?!\s+(?:has|have)\s+(?:quoted|offered))|"
    r"(?:not|cannot|can['’]t|won['’]t|don['’]t|unable to)\s+"
    r"(?:yet\s+)?(?:go ahead|accept(?:ed)?|book(?:ed)?|proceed|confirm(?:ed)?|take it|move forward|be moving forward)|"
    r"nicht (?:buchen|annehmen|fortfahren)|leider nicht(?=\s*(?:[.!,;]|$))|cancel|absage|ablehnen|zu teuer|kein interesse|"
    r"decided against|cheaper (?:option|offer|rate|quote) elsewhere|found a cheaper|trop cher|pas donner suite)\b",
    re.IGNORECASE,
)
# In the clause of the accept phrase: "we have decided not to go ahead", "Bitte buchen Sie nicht", "go ahead with a
# competitor", "go ahead once we confirm pickup" decline or hold; rounds 9, 10 and 15 were booked on such a reply.
# An English negation counts before the phrase only: "book it and don't forget the AWB" still books.
NEGATED = re.compile(r"\b(?:not|no need|never)\b|n['’]t\b", re.IGNORECASE)
HOLD = re.compile(
    r"\b(?:nicht|nichts|kein\w*|erst|hold|competitor|other (?:quote|offer|forwarder)|anderweitig|storniert"
    r"|once (?:we|our|you have)|only after|sobald|nach\s+(?:unserer|der)\s+freigabe|next week)\b",
    re.IGNORECASE,
)
CONDITION = re.compile(
    r"\b(?:if|unless|whether|falls|wenn|ob|haven['’]t|hasn['’]t|have not|has not|didn['’]t|did not)\b", re.IGNORECASE
)
ASKS_TO_BOOK = re.compile(
    r"\b(?:can|could|would|will|können|könnten)\s+(?:you|sie)\s+(?:please\s+|bitte\s+)?(?:book|proceed|buchen)",
    re.IGNORECASE,
)
CONFIRMED = re.compile(r"booking\s+(\S+)\s+(?:is\s+)?confirmed", re.IGNORECASE)
AIRLINE_REFERENCE = re.compile(r"\bbooking reference:?\s*(\S+)", re.IGNORECASE)
OURS = re.compile(r"\byour reference:?\s*(\S+?)[.,;]?\s*$", re.IGNORECASE | re.MULTILINE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
REQUEST_WAIT_S, REQUEST_POLL_S = 20.0, 0.5
REBOOKABLE = (404, 409)  # a portal answer to a booking that a fresh rate id can fix


def intent(text: str) -> str:
    """accept, decline or other, by rules. A decline wins over an accept word ("we cannot accept"); an acceptance
    counts in a sentence that is no condition ("if the price holds, book it") and no question, unless the question
    asks us to book ("Could you book it?")."""
    if DECLINE.search(text):
        return "decline"
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", text):
        if CONDITION.search(sentence) or (sentence.rstrip().endswith("?") and not ASKS_TO_BOOK.search(sentence)):
            continue
        for clause in re.split(r"[,;:]|\s[-–]\s|\bbut\b", sentence):
            if (m := ACCEPT.search(clause)) and not NEGATED.search(clause[: m.start()]) and not HOLD.search(clause):
                return "accept"
    return "other"


async def reply_intent(ctx: Context, message: Message) -> str:
    """The reply's own words decide; only when they say nothing does an attachment (a signed offer, a letter)."""
    own = followup.own_words(message)
    subject = "" if extract.REPLY_SUBJECT.match(message.subject) else message.subject
    said = intent(own or subject)
    # A reply that asks something ("Bleibt der Preis gleich?") wants a person, whatever its attachment says.
    if said == "other" and message.attachments and "?" not in own:
        said = intent("\n".join(line for _, lines in await extract._attachments(ctx, message) for line in lines))
    return said


async def handle_reply(ctx: Context, message: Message) -> None:
    """The customer wrote in their thread, before or after our quote: `followup.read` reads what it says and
    `followup.action` decides. A change before the acceptance is priced again and answered (the answer to the change
    mail counts, round 86); an acceptance books; a cancellation after the booking cancels it; a decline closes; a
    tracking question after the acceptance is answered from the CRM; a thank-you needs nobody; the rest a person
    answers."""
    quote = quotes.for_session(ctx, message.session_id)
    said = await reply_intent(ctx, message)
    sent = quote if quote is not None and quote.sent_at is not None else None
    rfq = await _request(ctx, message.session_id, quote)
    read = await followup.read(ctx, message, sent, rfq, said)
    act = followup.action(read)
    if act == "requote" and rfq is not None:
        from pixel.agents.rfq import agent  # agent imports core.poller, which imports this module

        await agent.requote(ctx, message, rfq.model_copy(update=read.changes))
        return
    if act == "track":
        from pixel.agents.tracking import agent as tracking

        await tracking.handle(ctx, message)
        return
    if act == "nothing" or (read.intent == "thanks" and read.phase == "open" and _acknowledges_no_price(ctx, message)):
        return
    if act == "cancel":
        await cancel(ctx, sent, message)
        return
    if act == "book_if" and sent is not None:
        if read.cap is None or not selection.within_cap(sent.total, sent.currency, *read.cap):
            events.emit(
                ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
                payload={"note": "Customer books on a condition our quote does not meet or code cannot read",
                         "cap": [str(c) for c in read.cap] if read.cap else None, "total": str(sent.total)},
            )  # fmt: skip
            return
        act = "book"
    if quote is not None and sent is None:
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer replied to an unsent draft", "intent": said, "quote_id": quote.id},
        )  # fmt: skip
        return
    if sent is None or act in ("person", "requote"):
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply needs a person", "intent": read.intent, "phase": read.phase,
                     "changes": {k: str(v) for k, v in read.changes.items()}, "has_quote": quote is not None},
        )  # fmt: skip
        return
    if act == "decline":
        quotes.set_status(ctx, sent.id, QuoteStatus.DECLINED)
        events.emit(ctx, "customer.declined", "crm", session_id=sent.session_id, message_id=message.id)
        return
    if act == "rebook":
        original = ctx.db.execute(
            "SELECT message_id FROM events WHERE session_id = ? AND kind = 'customer.accepted' ORDER BY id LIMIT 1",
            (sent.session_id,),
        ).fetchone()
        if original and original[0] == message.id:
            await book(ctx, sent.id, reply_to=message.id, resume=True)
        return
    with db.atomic(ctx.db):
        quotes.set_status(ctx, sent.id, QuoteStatus.ACCEPTED)
        events.emit(ctx, "customer.accepted", "crm", session_id=sent.session_id, message_id=message.id)
    await book(ctx, sent.id, reply_to=message.id)


def _acknowledges_no_price(ctx: Context, message: Message) -> bool:
    if message.in_reply_to is None or message.attachments:
        return False
    own = followup.own_words(message)
    if not NO_PRICE_ACK.fullmatch(extract._unsigned(own).strip() or own):
        return False
    return (
        ctx.db.execute(
            "SELECT 1 FROM messages WHERE id=? AND session_id=? AND direction='out' AND purpose='no_price'",
            (message.in_reply_to, message.session_id),
        ).fetchone()
        is not None
    )


async def _request(ctx: Context, session_id: int, quote: Quote | None) -> Rfq | None:
    """The request as the session knows it now: the newest reading (a change priced again moves it), else the quote's.
    Before our quote the request can still be in reading: a change that arrives then waits for it."""
    for _ in range(int(REQUEST_WAIT_S / REQUEST_POLL_S)):
        if found := session.rfq(ctx, session_id) or (quote.rfq if quote else None):
            return found
        if quote is not None:
            return None
        await asyncio.sleep(REQUEST_POLL_S)
    return None


async def book(ctx: Context, quote_id: str, reply_to: int | None = None, *, resume: bool = False) -> Booking:
    """Creates the shipment (its CRM id is the `team_reference`), books by `rate_id`; on 410, 404 or 409 fetches a
    fresh rate and books that; on 503, without a rate_id or when no fresh rate comes (the rate API down or capped)
    books by email with the carrier. Tells the customer."""
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

    if booking.carrier.casefold() in availability.suspended_carriers(ctx, quote.rfq):
        return _failed(ctx, booking, "carrier suspended new bookings on this lane")
    if booking.via == "email" or not booking.rate_id:
        return await _book_by_mail(ctx, quote, booking, "booking by email")
    try:
        done = await _portal_book(ctx, booking.rate_id, booking.team_reference)
    except BookingUnavailable as e:
        return await _book_by_mail(ctx, quote, booking, e.hint)
    except (RateExpired, ApiError) as e:
        # 410 expired, 404 unknown (a rate id from the desk or the browser page the bookings API does not know),
        # 409 booked under another reference: a fresh rate for the same airline books instead.
        if isinstance(e, ApiError) and e.status not in REBOOKABLE:
            return _failed(ctx, booking, repr(e))
        why = "rate expired" if isinstance(e, RateExpired) else f"portal booking {e.status}"
        try:
            fresh = await portal_rates.refresh(ctx, quote.option)
            events.emit(
                ctx, "option.found", "crm", session_id=quote.session_id,
                payload={"why": f"{why}, fresh rate for booking", "option": fresh.model_dump(mode="json")},
            )  # fmt: skip
            booking = booking.model_copy(update={"rate_id": fresh.rate_id})
            _save(ctx, booking, "booking")
            done = await _portal_book(ctx, fresh.rate_id, booking.team_reference)
        except BookingUnavailable as e2:
            return await _book_by_mail(ctx, quote, booking, e2.hint)
        except (RateExpired, NotOnPortal, RateLimited, ApiError) as e2:
            # The rate API is down or capped while the customer's rate id expired (the 503 streak since round 93):
            # the airline books by mail. A rate hint names the desk, never an airline, so its address is not used.
            return await _book_by_mail(ctx, quote, booking, f"{why}, no fresh rate: {type(e2).__name__}")
        except httpx.HTTPError as e2:
            return _failed(ctx, booking, f"fresh rate: {e2!r}")
    except httpx.HTTPError as e:
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


async def cancel(ctx: Context, quote: Quote, message: Message) -> None:
    """The customer withdrew after the booking (round 88: "please cancel booking BK-…"): the portal cancels it, the
    CRM marks booking and shipment cancelled, the customer hears so once. Never a second quote. A booking the portal
    cannot cancel (one booked by mail without an airline reference yet, or the portal refusing or down) is withdrawn
    by mail with the airline it was booked with; only without an address does a person take it."""
    booking = bookings.for_session(ctx, quote.session_id)
    if booking is None:
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer cancels a booking we do not hold", "intent": "decline"},
        )  # fmt: skip
        return
    if booking.status != BookingStatus.CANCELLED:
        why = None
        if not booking.booking_reference:
            why = "no airline reference yet"
        elif not ctx.settings.dry_run:
            try:
                await ctx.portal.cancel(booking.booking_reference)
            except (ApiError, httpx.HTTPError) as e:
                why = f"portal refused: {e!r}"
        if why and not await _cancel_by_mail(ctx, quote, booking, why):
            events.emit(
                ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
                payload={"note": "Customer cancels; neither the portal nor a mail to the airline could", "why": why},
            )  # fmt: skip
            return
        booking = booking.model_copy(update={"status": BookingStatus.CANCELLED})
        _save(ctx, booking, "cancelled")
        events.emit(
            ctx, "booking.cancelled", "crm", session_id=quote.session_id, message_id=message.id,
            payload={"booking_reference": booking.booking_reference, "shipment": booking.team_reference,
                     "via": "email" if why else "portal", **({"why": why} if why else {})},
        )  # fmt: skip
    await _tell_customer(ctx, quote, message.id, "cancelled", airline=booking.carrier,
                         reference=booking.team_reference, airline_reference=booking.booking_reference or booking.team_reference)  # fmt: skip


async def _cancel_by_mail(ctx: Context, quote: Quote, booking: Booking, why: str) -> bool:
    address = _airline_address(ctx, booking.carrier, "")
    if address is None:
        return False
    reference = f" (your reference {booking.booking_reference})" if booking.booking_reference else ""
    body = "\n".join([
        "Hello,", "",
        f"please cancel our booking {booking.team_reference}{reference}: {_lane(quote)}, {quote.rfq.weight_kg} kg.",
        "Our customer has withdrawn the order.", "", "Please confirm the cancellation.", "", "Thank you,",
        "PIXEL Air Freight",
    ])  # fmt: skip
    try:
        await outbox.send(
            ctx, to=address, subject=f"Cancellation {booking.team_reference}: {_lane(quote)}", body=body,
            session_id=quote.session_id, purpose="booking",
        )  # fmt: skip
    except outbox.NotSent:
        return False
    return True


async def _notify_booked(ctx: Context, quote: Quote, booking: Booking, reply_to: int | None) -> None:
    await _tell_customer(ctx, quote, reply_to, "booked", airline=booking.carrier,
                         price=compose.money(quote.total), currency=quote.currency,
                         reference=booking.team_reference, airline_reference=booking.booking_reference or "-")  # fmt: skip


async def _confirmation_text(ctx: Context, message: Message) -> str:
    attached = await extract._attachments(ctx, message) if message.attachments else []
    own = extract.HISTORY.split(message.body, maxsplit=1)[0]
    return "\n".join([own, *(line for _, lines in attached for line in lines)])


async def confirmed_references(ctx: Context, message: Message) -> tuple[str | None, str | None]:
    """The airline's booking reference and ours, from the subject, the body or an attached confirmation."""
    return _references(message.subject, await _confirmation_text(ctx, message))


def _references(subject: str, text: str) -> tuple[str | None, str | None]:
    match = CONFIRMED.search(subject) or CONFIRMED.search(text) or AIRLINE_REFERENCE.search(text)
    # A booking by mail has no airline reference yet; its confirmation names ours (round 27: "Your reference: PX-…").
    ours = OURS.search(text)
    return (match.group(1).strip(".,:;") if match else None), (ours.group(1) if ours else None)


async def handle_confirmation(ctx: Context, message: Message) -> None:
    """ "Booking <reference> confirmed" from the airline -> booking confirmed, shipment updated, customer told. An
    answer that refuses ("could not be confirmed", in the mail or its attachment) fails the booking for a person."""
    # router imports crm, which this module imports too
    from pixel.agents.router.agent import BOOKED, REFUSED, booking_pending

    text = await _confirmation_text(ctx, message)
    reference, ours = _references(message.subject, text)
    booking = (
        (reference and bookings.by_reference(ctx, reference))
        or (ours and bookings.by_team_reference(ctx, ours))
        or bookings.for_session(ctx, message.session_id)
    )
    if booking is None:
        events.emit(
            ctx, "error", "crm", session_id=message.session_id, message_id=message.id,
            payload={"error": "confirmation for an unknown booking", "booking_reference": reference},
        )  # fmt: skip
        return
    if booking.status == BookingStatus.CANCELLED:  # the airline's late confirmation of a booking the customer withdrew
        return
    if booking.status != BookingStatus.CONFIRMED and REFUSED.search(text):
        _failed(ctx, booking, "the airline refused the booking")
        events.emit(
            ctx, "human.note", "crm", session_id=booking.session_id, message_id=message.id,
            payload={"note": "The airline refused our booking: book another option", "shipment": booking.team_reference},
        )  # fmt: skip
        return
    if booking_pending(f"{message.subject}\n{text}"):
        if (
            booking.status == BookingStatus.REQUESTED
            and not booking.booking_reference
            and reference
            and re.fullmatch(r"BK-[A-Z0-9]{4,}", reference, re.IGNORECASE)
        ):
            bookings.save(ctx, booking.model_copy(update={"booking_reference": reference}))
        events.emit(ctx, "human.note", "crm", session_id=booking.session_id, message_id=message.id,
                    payload={"note": "Airline response does not confirm the whole booking",
                             "shipment": booking.team_reference, "booking_reference": reference})  # fmt: skip
        return
    # Only what says so confirms: an answer in our booking thread with a form, terms or "request received" attached
    # reaches this handler too and confirms nothing.
    if not (CONFIRMED.search(message.subject) or BOOKED.search(text) or AIRLINE_REFERENCE.search(text)):
        events.emit(
            ctx, "human.note", "crm", session_id=booking.session_id, message_id=message.id,
            payload={"note": "The airline answered our booking without confirming it", "shipment": booking.team_reference},
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
    """The address the portal's message names, else the carrier list's. The rate desk and the tracking desk book
    nothing."""
    for found in EMAIL.finditer(hint or ""):
        if (address := found.group(0).rstrip(".")).lower() not in (RATEDESK, TRACKING):
            return address
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
        "booked": "Thanks for the go-ahead. Your shipment from {origin} to {destination} ({weight} kg) is now booked "
                  "with {airline} at {price} {currency} {scope}.\n\nOur reference for you is {reference}, the "
                  "airline's is {airline_reference}. I'll let you know as soon as {airline} confirms.",
        "confirmed": "Good news: {airline} has confirmed your booking {airline_reference} from {origin} to "
                     "{destination} ({weight} kg).\n\nYour reference with us stays {reference}. If you'd like "
                     "to know where your shipment is, just ask.",
        "cancelled": "As requested, we have cancelled your booking {airline_reference} with {airline} from {origin} "
                     "to {destination} ({weight} kg). Your reference {reference} is closed.",
    },
    "de": {
        "booked": "Danke für Ihre Zusage. Ihre Sendung von {origin} nach {destination} ({weight} kg) ist jetzt bei "
                  "{airline} gebucht, zu {price} {currency} {scope}.\n\nUnsere Referenz für Sie ist {reference}, "
                  "die der Airline {airline_reference}. Ich melde mich, sobald {airline} bestätigt.",
        "confirmed": "Gute Nachricht: {airline} hat Ihre Buchung {airline_reference} von {origin} nach "
                     "{destination} ({weight} kg) bestätigt.\n\nIhre Referenz bei uns bleibt {reference}. Wenn "
                     "Sie wissen möchten, wo Ihre Sendung gerade ist, fragen Sie einfach.",
        "cancelled": "Wie gewünscht haben wir Ihre Buchung {airline_reference} bei {airline} von {origin} nach "
                     "{destination} ({weight} kg) storniert. Ihre Referenz {reference} ist geschlossen.",
    },
    "fr": {
        "booked": "Merci pour votre accord. Votre envoi de {origin} à {destination} ({weight} kg) est maintenant "
                  "réservé avec {airline} à {price} {currency} {scope}.\n\nNotre référence pour vous est "
                  "{reference}, celle de la compagnie {airline_reference}. Je reviens vers vous dès que {airline} "
                  "confirme.",
        "confirmed": "Bonne nouvelle : {airline} a confirmé votre réservation {airline_reference} de {origin} à "
                     "{destination} ({weight} kg).\n\nVotre référence chez nous reste {reference}. Si vous souhaitez "
                     "savoir où se trouve votre envoi, il suffit de demander.",
        "cancelled": "Comme demandé, nous avons annulé votre réservation {airline_reference} avec {airline} de "
                     "{origin} à {destination} ({weight} kg). Votre référence {reference} est clôturée.",
    },
}  # fmt: skip
# The quote's voice carries on into its booking mail: a thank-you a person adds, once per thread.
THANKS = {
    "en": " Thanks again, great to have you on board! 😊",
    "de": " Nochmals danke, schön, dass Sie mit uns fliegen! 😊",
    "fr": " Encore merci, ravis de vous compter parmi nos clients ! 😊",
}
AIR_ONLY = {
    "en": "for airport-to-airport transport, excluding pickup",
    "de": "für den Flughafen-zu-Flughafen-Transport ohne Abholung",
    "fr": "pour le transport d'aéroport à aéroport, sans enlèvement",
}


TRANSLATE_WAIT_S = 15


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
    request = compose.own_words(compose.customer_body(ctx, quote.rfq.message_id))
    name = (prose.first_name if prose else None) or compose.signed_name(request, ctx.settings.signer)
    bye = compose.sign_off(request, lang, name)[1]
    scope = "all-in"
    if quote.rfq.door_pickup and not quote.option.trucker:
        scope = AIR_ONLY[lang]
    text = t[what].format(scope=scope, origin=quote.option.origin, destination=quote.option.destination,
                          weight=compose.weight(quote.rfq.weight_kg, lang), **facts)  # fmt: skip
    if what == "booked" and compose.register(request) != "formal":  # once per thread; a formal mail ends calm
        text += THANKS[lang]
    body = "\n\n".join([compose.greeting(lang, name, request), text,
                        compose.signature(bye, ctx.settings.signer)])  # fmt: skip
    if lang != quote.rfq.language:  # the thread's language, also when the customer's reply is English
        english, names = body, (quote.option.origin, quote.option.destination, *facts.values())
        body = await compose.localize(ctx, quote.rfq.language, quote.session_id, body, TRANSLATE_WAIT_S,
                                      lambda text: compose.kept(english, text, names), "booking.translate")  # fmt: skip
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
