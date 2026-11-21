"""From the customer's acceptance to the airline's confirmation (R22–R24). Every step is an event; mail leaves
through `core.outbox` only."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from pixel.agents.rfq import compose, extract
from pixel.api.client import ApiError
from pixel.api.portal import BookingUnavailable, NotOnPortal, PortalBooking, RateExpired, RateLimited
from pixel.contracts import Booking, BookingStatus, Message, Quote, QuoteStatus, RateOption, Source
from pixel.core import db, events, outbox, timing
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import calc, portal_rates, store

# The airline's price for the accepted quote is read again right before booking: a change beyond a cent asks the
# customer first (R22, P1). A price the customer confirmed may change again; after the third question a person decides.
PRICE_TOLERANCE = Decimal("0.01")
MAX_RECONFIRM_ATTEMPTS = 3

ACCEPT = re.compile(
    r"\b(?:(?:we|i)\s+(?:hereby\s+)?(?:accept|approve)(?!\s+that)|accept(?:ed)?\s+(?:it|(?:this|your|the)\s+(?:offer|quote|quotation|rate))"
    r"|(?:offer|quote) accepted|confirm\s+(?:the|this|our)\s+booking|go ahead|book\s+(?:it|this|the\s+shipment|with)\b"
    r"|please\s+(?:book|proceed)(?=\s*(?:[.,!;]|$|\s+(?:it|this|the|with|now|as|and|our|right)\b))|let['’]s (?:book|proceed|do it)|we['’]ll take (?:it|the offer)|(?<!as )agreed"
    r"|(?:would|['’]d)\s+like\s+to\s+(?:book|proceed)|arrange\s+the\s+booking"
    r"|bitte\s+(?:\w+\s+){0,2}buchen|buchen\s+sie|gerne\s+buchen|sie\s+können\s+buchen|auftrag|einverstanden"
    r"|nehmen\s+(?:ihr|das)\s+angebot\s+an|angebot\s+angenommen|bitte\s+fortfahren|buchung\s+vornehmen)",
    re.IGNORECASE,
)
DECLINE = re.compile(
    r"\b(decline|no thanks|(?<!not )too (?:low|high)|not competitive|pass on (?:this|it|that|your offer)|not interested|"
    r"(?<!not )too expensive|went with|"
    r"another (?:offer|forwarder)|"
    r"(?:not|cannot|can['’]t|won['’]t|don['’]t|unable to)\s+"
    r"(?:yet\s+)?(?:go ahead|accept(?:ed)?|book(?:ed)?|proceed|confirm(?:ed)?|take it|move forward|be moving forward)|"
    r"nicht (?:buchen|annehmen|fortfahren)|leider nicht(?=\s*(?:[.!,;]|$))|cancel|absage|ablehnen|zu teuer|kein interesse)\b",
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
# The answer to our question about a changed price: a bare "Yes." or "Nein." is clear there, not after a quote.
RECONFIRM_DECLINE = re.compile(
    r"\b(?:nein,?\s*danke|no,\s*thanks|over (?:our )?budget|skip (?:it|this)|leave it|don['’]?t want|do not want"
    r"|not at (?:this|that) price|nicht zu diesem preis|stornieren|abbrechen)\b|^(?:no|nein)[.,!]?$",
    re.IGNORECASE,
)
RECONFIRM_ACCEPT = re.compile(
    r"\bconfirm(?:ed)?\s+(?:the\s+)?(?:new|updated|revised)\s+(?:price|rate|total|quote)|\breconfirm"
    r"|\bbestätige(?:n)?\s+(?:den\s+)?(?:neuen|aktualisierten)\s+preis"
    r"|^\s*(?:yes|ja)\b[.,!]?(?:\s+(?:please|bitte|that works|that['’]s fine|ok(?:ay)?|confirmed|sure|fine|das passt"
    r"|in ordnung|passt|gerne|bestätigt))?"
    r"|\bthat['’]s (?:fine|ok|okay|acceptable)\b|\b(?:fine|ok|okay) with (?:the )?(?:new|updated) (?:price|rate|total)"
    r"|\bproceed (?:at|with) (?:the )?(?:new|updated) (?:price|rate|total)",
    re.IGNORECASE,
)


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


def reconfirm_intent(text: str) -> str:
    """accept, decline or other for the reply to a changed booking price. What `intent` reads stands; a short
    "Yes." or "No." counts here too, still not in a condition, a question, a negated or a held sentence."""
    said = intent(text)
    if said in ("accept", "decline"):
        return said
    if RECONFIRM_DECLINE.search(text.strip()):
        return "decline"
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", text):
        if CONDITION.search(sentence) or HOLD.search(sentence):
            continue
        if sentence.rstrip().endswith("?") and not ASKS_TO_BOOK.search(sentence):
            continue
        for clause in re.split(r"[,;:]|\s[-–]\s|\bbut\b", sentence):
            if (m := RECONFIRM_ACCEPT.search(clause)) and not NEGATED.search(clause[: m.start()]):
                return "accept"
    return "other"


@dataclass(frozen=True)
class _PriceChange:
    rate_id: str | None
    old_air: Decimal
    new_air: Decimal
    old_total: Decimal
    new_total: Decimal
    old_currency: str
    new_currency: str
    why: str
    option: RateOption | None = None


def _reconfirm_key(quote_id: str) -> str:
    return f"booking-reconfirm:{quote_id}"


def _load_reconfirm(ctx: Context, quote_id: str) -> dict | None:
    raw = db.get(ctx.db, _reconfirm_key(quote_id))
    return json.loads(raw) if raw else None


def _save_reconfirm(ctx: Context, quote_id: str, state: dict) -> None:
    db.put(ctx.db, _reconfirm_key(quote_id), json.dumps(state))


def _approval(quote: Quote, approved: dict | None) -> tuple[Decimal, Decimal, str, str | None]:
    """Air price, all-in total, currency and rate_id the customer agreed to: the quote's, or the new price they
    confirmed."""
    if approved is None:
        return calc.air_price(quote.option), quote.total, quote.currency, quote.option.rate_id
    return (Decimal(approved["air_price"]), Decimal(approved["total"]), approved["currency"],
            approved.get("rate_id") or quote.option.rate_id)  # fmt: skip


def _compare_option(
    quote: Quote, fresh: RateOption, approved_air: Decimal, approved_total: Decimal, approved_currency: str, why: str
) -> _PriceChange | None:
    """The fresh airline price against the approved one, in the approved currency; the quote's pickup stays."""
    old_air = calc.money(approved_air)
    converted = fresh
    if fresh.currency != approved_currency:
        converted = calc.in_currency(fresh, approved_currency, quote.option.fx_eur)
    if converted.currency == approved_currency:
        new_air, new_currency = calc.money(converted.total), approved_currency
        new_total = calc.money(new_air + quote.option.pickup)
    else:
        new_air, new_currency = calc.money(fresh.total), fresh.currency
        new_total = new_air
    if new_currency == approved_currency and abs(new_air - old_air) <= PRICE_TOLERANCE:
        return None
    return _PriceChange(rate_id=fresh.rate_id, old_air=old_air, new_air=new_air, old_total=calc.money(approved_total),
                        new_total=new_total, old_currency=approved_currency, new_currency=new_currency, why=why,
                        option=fresh)  # fmt: skip


def _actual_booked_total(
    quote: Quote, done: PortalBooking | Booking, approved_air: Decimal, approved_total: Decimal, approved_currency: str
) -> tuple[Decimal, str, bool]:
    """The all-in total the airline's booked price makes, and whether it differs from the approved one."""
    if done.price is None:
        return approved_total, approved_currency, False
    if done.currency == approved_currency:
        booked_air = calc.money(done.price)
        if abs(booked_air - calc.money(approved_air)) <= PRICE_TOLERANCE:
            return approved_total, approved_currency, False
        return calc.money(booked_air + quote.option.pickup), approved_currency, True
    fx = quote.option.fx_eur  # EUR per unit of the approved currency, when that is not EUR
    if done.currency == "EUR" and fx:
        return calc.money(done.price / fx + quote.option.pickup), approved_currency, True
    return calc.money(done.price), done.currency, True


async def _verify_price(
    ctx: Context,
    quote: Quote,
    approved_air: Decimal,
    approved_total: Decimal,
    approved_currency: str,
    *,
    rate_id: str | None,
) -> tuple[RateOption | None, _PriceChange | None]:
    """The airline's current price for the approved booking: the portal rate read again, or the newest tariff.
    Without an answer the booking goes ahead as approved; a dry run reads nothing."""
    if ctx.settings.dry_run:
        return None, None
    if rate_id and ctx.portal is not None:
        try:
            fresh = await portal_rates.refresh(ctx, quote.option)
        except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError):
            return None, None
        if (fresh.origin, fresh.destination) != (quote.option.origin, quote.option.destination):
            return None, None  # a rate for another lane is nothing to compare by
        why = "portal rate changed before booking"
        return fresh, _compare_option(quote, fresh, approved_air, approved_total, approved_currency, why)
    if rate_id or quote.option.source != Source.SHEET:
        return None, None
    tariffs = store.lookup(ctx, quote.option.origin, quote.option.destination, on=timing.now().date())
    tariff = next((t for t in tariffs if t.carrier.lower() == quote.option.carrier.lower()), None)
    if tariff is None:
        return None, None
    fresh = calc.price_tariff(tariff, quote.rfq)
    if calc.in_currency(fresh, approved_currency, quote.option.fx_eur).currency != approved_currency:
        return None, None  # no exchange rate to compare by: the quoted price stands
    why = "tariff updated before booking"
    return fresh, _compare_option(quote, fresh, approved_air, approved_total, approved_currency, why)


async def _price_changed(
    ctx: Context, quote: Quote, change: _PriceChange, *, reply_to: int | None, trigger_message_id: int | None
) -> None:
    """Asks the customer about the new price, or hands the booking to a person once it changed too often."""
    state = _load_reconfirm(ctx, quote.id)
    attempts = int(state.get("attempts", 0)) + 1 if state else 1
    if attempts <= MAX_RECONFIRM_ATTEMPTS:
        await _request_reconfirm(ctx, quote, change, reply_to=reply_to, trigger_message_id=trigger_message_id,
                                 attempts=attempts)  # fmt: skip
        return
    _save_reconfirm(ctx, quote.id, {**(state or {}), "status": "held"})
    events.emit(
        ctx, "human.note", "crm", session_id=quote.session_id, message_id=trigger_message_id,
        payload={"note": "Booking price keeps changing; a person decides", "why": change.why,
                 "new_total": str(change.new_total), "currency": change.new_currency, "attempts": attempts - 1},
    )  # fmt: skip


async def _request_reconfirm(
    ctx: Context, quote: Quote, change: _PriceChange, *, reply_to: int | None, trigger_message_id: int | None,
    attempts: int,
) -> None:  # fmt: skip
    state = {
        "status": "pending", "quote_id": quote.id, "rate_id": change.rate_id, "air_price": str(change.new_air),
        "total": str(change.new_total), "currency": change.new_currency, "old_air_price": str(change.old_air),
        "old_total": str(change.old_total), "old_currency": change.old_currency,
        "trigger_message_id": trigger_message_id, "attempts": attempts,
    }  # fmt: skip
    _save_reconfirm(ctx, quote.id, state)
    if change.option is not None:
        events.emit(ctx, "option.found", "crm", session_id=quote.session_id,
                    payload={"why": change.why, "option": change.option.model_dump(mode="json")})  # fmt: skip
    events.emit(
        ctx, "human.note", "crm", session_id=quote.session_id, message_id=trigger_message_id,
        payload={"note": "Booking price changed; awaiting customer reconfirmation", "why": change.why,
                 "old_total": str(change.old_total), "new_total": str(change.new_total),
                 "currency": change.new_currency, "attempt": attempts},
    )  # fmt: skip
    await _ask_reconfirm(ctx, quote, state, reply_to)


async def _ask_reconfirm(ctx: Context, quote: Quote, state: dict, reply_to: int | None) -> None:
    """Once per attempt: a replayed reply finds the question sent already."""
    await _tell_customer(
        ctx, quote, reply_to, "reconfirm", notice_key=f"booking-notice:{quote.id}:reconfirm:{state['attempts']}",
        airline=quote.option.carrier, old_price=compose.money(Decimal(state["old_total"])),
        old_currency=state["old_currency"], new_price=compose.money(Decimal(state["total"])),
        new_currency=state["currency"],
    )  # fmt: skip


async def _handle_reconfirm_reply(ctx: Context, quote: Quote, message: Message, pending: dict, text: str) -> None:
    """The customer answers our question about the changed price: yes books at that price, no books nothing."""
    if pending.get("trigger_message_id") == message.id:  # the reply that made us ask, delivered again
        await _ask_reconfirm(ctx, quote, pending, message.id)
        return
    said = reconfirm_intent(text)
    if said == "other":
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply to the new booking price needs a person", "intent": said},
        )  # fmt: skip
        return
    if said == "decline":
        with db.atomic(ctx.db):
            _save_reconfirm(ctx, quote.id, {**pending, "status": "declined"})
            quotes.set_status(ctx, quote.id, QuoteStatus.DECLINED)
            existing = bookings.for_session(ctx, quote.session_id)
            if existing and existing.status == BookingStatus.REQUESTED and existing.booking_reference is None:
                _save(ctx, existing.model_copy(update={"status": BookingStatus.FAILED}), "declined")
            events.emit(ctx, "customer.declined", "crm", session_id=quote.session_id, message_id=message.id,
                        payload={"after_price_change": True})  # fmt: skip
        await _tell_customer(ctx, quote, message.id, "cancelled", airline=quote.option.carrier)
        return
    air, total, currency, rate_id = _approval(quote, pending)
    fresh, change = await _verify_price(ctx, quote, air, total, currency, rate_id=rate_id)
    if change is not None:
        await _price_changed(ctx, quote, change, reply_to=message.id, trigger_message_id=message.id)
        return
    confirmed = {**pending, "status": "confirmed", "accepted_message_id": message.id}
    if fresh is not None and fresh.rate_id:
        confirmed["rate_id"] = fresh.rate_id
    with db.atomic(ctx.db):
        _save_reconfirm(ctx, quote.id, confirmed)
        quotes.set_status(ctx, quote.id, QuoteStatus.ACCEPTED)
        events.emit(ctx, "customer.accepted", "crm", session_id=quote.session_id, message_id=message.id,
                    payload={"after_price_change": True, "total": confirmed["total"], "currency": currency})  # fmt: skip
    await book(ctx, quote.id, reply_to=message.id, resume=True, _prefetched=fresh, _checked=True, _approved=confirmed)


async def handle_reply(ctx: Context, message: Message) -> None:
    """The customer answered our quote: acceptance -> book; decline -> quote declined; else a person answers."""
    quote = quotes.for_session(ctx, message.session_id)
    # A reply written below the quoted mail keeps its own lines after the quote.
    unquoted = "\n".join(line for line in message.body.splitlines() if not line.lstrip().startswith(">"))
    own = extract.HISTORY.split(unquoted, maxsplit=1)[0].strip()
    subject = "" if extract.REPLY_SUBJECT.match(message.subject) else message.subject
    said = intent(own or subject)
    if quote is not None and quote.sent_at is None and quote.status == QuoteStatus.DRAFT:
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer replied to an unsent draft", "intent": said, "quote_id": quote.id},
        )  # fmt: skip
        return
    reconfirm = _load_reconfirm(ctx, quote.id) if quote is not None else None
    if reconfirm and reconfirm.get("status") == "pending":  # we asked about a changed price; this is the answer
        await _handle_reconfirm_reply(ctx, quote, message, reconfirm, own or subject)
        return
    if reconfirm and reconfirm.get("status") == "held" and quote.status != QuoteStatus.ACCEPTED:
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply while a person decides on the booking price", "intent": said},
        )  # fmt: skip
        return
    if quote is None or said == "other":
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply needs a person", "intent": said, "has_quote": quote is not None},
        )  # fmt: skip
        return
    if said == "decline" and quote.status == QuoteStatus.ACCEPTED:  # booked already: a person cancels, not a rule
        events.emit(
            ctx, "human.note", "crm", session_id=message.session_id, message_id=message.id,
            payload={"note": "Customer reply after the booking reads as a decline", "intent": said},
        )  # fmt: skip
        return
    if said == "decline":
        quotes.set_status(ctx, quote.id, QuoteStatus.DECLINED)
        events.emit(ctx, "customer.declined", "crm", session_id=quote.session_id, message_id=message.id)
        return
    if quote.status == QuoteStatus.ACCEPTED:
        original = ctx.db.execute(
            "SELECT message_id FROM events WHERE session_id = ? AND kind = 'customer.accepted' ORDER BY id DESC LIMIT 1",
            (quote.session_id,),
        ).fetchone()
        if original and original[0] == message.id:
            confirmed = reconfirm if reconfirm and reconfirm.get("status") == "confirmed" else None
            await book(ctx, quote.id, reply_to=message.id, resume=True, _approved=confirmed)
        return
    # The airline's price may have moved since the quote: the customer agrees to a new one before we book it.
    fresh, change = await _verify_price(
        ctx, quote, calc.air_price(quote.option), quote.total, quote.currency, rate_id=quote.option.rate_id
    )
    if change is not None:
        await _price_changed(ctx, quote, change, reply_to=message.id, trigger_message_id=message.id)
        return
    with db.atomic(ctx.db):
        quotes.set_status(ctx, quote.id, QuoteStatus.ACCEPTED)
        events.emit(ctx, "customer.accepted", "crm", session_id=quote.session_id, message_id=message.id)
    await book(ctx, quote.id, reply_to=message.id, _prefetched=fresh, _checked=True)


async def book(
    ctx: Context,
    quote_id: str,
    reply_to: int | None = None,
    *,
    resume: bool = False,
    _prefetched: RateOption | None = None,
    _checked: bool = False,
    _approved: dict | None = None,
) -> Booking:
    """Creates the shipment (its CRM id is the `team_reference`), books by `rate_id`; on 410 fetches a fresh
    rate and books that; on 503 or without a rate_id books by email with the carrier. Tells the customer.
    The price booked is the approved one: the quote's, or the new price the customer confirmed (`_approved`).
    A price that moved before the booking (`_verify_price`, or the fresh rate after 410) asks the customer first
    and leaves the booking requested; `_checked` says the caller has verified it, `_prefetched` is what it read."""
    quote = quotes.get(ctx, quote_id)
    if quote is None:
        raise KeyError(quote_id)
    approved_air, approved_total, approved_currency, approved_rate_id = _approval(quote, _approved)
    existing = bookings.for_session(ctx, quote.session_id)
    if existing and existing.status in (BookingStatus.BOOKED, BookingStatus.CONFIRMED):
        if resume and existing.status == BookingStatus.BOOKED:
            total, currency, changed = _actual_booked_total(quote, existing, approved_air, approved_total,
                                                            approved_currency)  # fmt: skip
            await _notify_booked(ctx, quote, existing, reply_to, total=total, currency=currency, changed=changed)
        return existing
    if existing and (
        (not resume and existing.status == BookingStatus.REQUESTED)
        or (resume and existing.status == BookingStatus.FAILED)
    ):
        return existing
    if existing:
        booking = existing
        if _approved is not None:  # a new price the customer confirmed after a 410: book that one
            booking = booking.model_copy(
                update={"rate_id": approved_rate_id, "price": approved_air, "currency": approved_currency}
            )
            _save(ctx, booking, "booking")
    else:
        shipment = shipments.for_session(ctx, quote.session_id) or shipments.create(ctx, quote)
        booking = Booking(
            id=f"B-{shipment.id}", quote_id=quote.id, session_id=quote.session_id, team_reference=shipment.id,
            via="portal", carrier=quote.option.carrier, rate_id=approved_rate_id, price=approved_air,
            currency=approved_currency, created_at=datetime.now(UTC),
        )  # fmt: skip
        _save(ctx, booking, "booking")
        events.emit(
            ctx, "booking.requested", "crm", session_id=quote.session_id,
            payload={"shipment": shipment.id, "rate_id": booking.rate_id, "carrier": booking.carrier},
        )  # fmt: skip

    if not _checked and booking.via != "email":  # a booking mail out already is the airline's to answer
        _prefetched, change = await _verify_price(ctx, quote, approved_air, approved_total, approved_currency,
                                                  rate_id=booking.rate_id)  # fmt: skip
        if change is not None:
            await _price_changed(ctx, quote, change, reply_to=reply_to, trigger_message_id=reply_to)
            return booking
    if booking.via == "email" or not booking.rate_id:
        return await _book_by_mail(ctx, quote, booking, "booking by email")
    try:
        done = await _portal_book(ctx, booking.rate_id, booking.team_reference)
    except RateExpired:
        try:
            if _prefetched is not None and _prefetched.rate_id and _prefetched.rate_id != booking.rate_id:
                fresh = _prefetched  # read for the price check a moment ago, at the approved price
            else:
                fresh = await portal_rates.refresh(ctx, quote.option)
                why = "rate expired, fresh rate changed price"
                change = _compare_option(quote, fresh, approved_air, approved_total, approved_currency, why)
                if change is not None:
                    await _price_changed(ctx, quote, change, reply_to=reply_to, trigger_message_id=reply_to)
                    return booking
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
    total, currency, changed = _actual_booked_total(quote, done, approved_air, approved_total, approved_currency)
    if changed:
        events.emit(
            ctx, "human.note", "crm", session_id=quote.session_id,
            payload={"note": "Airline booked at a price other than the approved one", "approved_total": str(approved_total),
                     "approved_currency": approved_currency, "booked_total": str(total), "currency": currency},
        )  # fmt: skip
    await _notify_booked(ctx, quote, booking, reply_to, total=total, currency=currency, changed=changed)
    return booking


async def _notify_booked(
    ctx: Context, quote: Quote, booking: Booking, reply_to: int | None, *, total: Decimal, currency: str,
    changed: bool = False,
) -> None:  # fmt: skip
    """The total the customer agreed to, or what the airline's booked price makes when it booked another one."""
    await _tell_customer(ctx, quote, reply_to, "booked_changed" if changed else "booked",
                         notice_key=f"booking-notice:{quote.id}:booked", airline=booking.carrier,
                         price=compose.money(total), currency=currency,
                         reference=booking.team_reference, airline_reference=booking.booking_reference or "-")  # fmt: skip


async def handle_confirmation(ctx: Context, message: Message) -> None:
    """ "Booking <reference> confirmed" from the airline -> booking confirmed, shipment updated, customer told."""
    match = (
        CONFIRMED.search(message.subject) or CONFIRMED.search(message.body) or AIRLINE_REFERENCE.search(message.body)
    )
    reference = match.group(1).strip(".,:;") if match else None
    # A booking by mail has no airline reference yet; its confirmation names ours (round 27: "Your reference: PX-…").
    ours = OURS.search(message.body)
    booking = (
        (reference and bookings.by_reference(ctx, reference))
        or (ours and bookings.by_team_reference(ctx, ours.group(1)))
        or bookings.for_session(ctx, message.session_id)
    )
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
        "booked": "Thanks for the go-ahead. Your shipment from {origin} to {destination} ({weight} kg) is now booked "
                  "with {airline} at {price} {currency} {scope}.\n\nOur reference for you is {reference}, the "
                  "airline's is {airline_reference}. I'll let you know as soon as {airline} confirms.",
        "booked_changed": "Thanks for the go-ahead. Your shipment from {origin} to {destination} ({weight} kg) is "
                          "now booked with {airline}, but the airline booked it at {price} {currency} {scope}, not "
                          "at the price we agreed. I'm checking this with {airline} and will come back to you.\n\n"
                          "Our reference for you is {reference}, the airline's is {airline_reference}.",
        "reconfirm": "Thanks for the go-ahead. Before I book: {airline} has changed its price for your shipment "
                     "from {origin} to {destination} ({weight} kg). It now comes to {new_price} {new_currency} "
                     "{scope} instead of the {old_price} {old_currency} I quoted.\n\nShall I book it at the new "
                     "price? A short yes is enough and I'll book right away. If it no longer works for you, just "
                     "let me know and I won't book anything.",
        "cancelled": "Understood, I won't book your shipment from {origin} to {destination} ({weight} kg) at the "
                     "new price, and nothing has been booked with {airline}.\n\nIf you'd like a new quote later "
                     "on, just reply to this mail.",
        "confirmed": "Good news: {airline} has confirmed your booking {airline_reference} from {origin} to "
                     "{destination} ({weight} kg).\n\nYour reference with us stays {reference}. If you'd like "
                     "to know where your shipment is, just ask.",
    },
    "de": {
        "booked": "Danke für Ihre Zusage. Ihre Sendung von {origin} nach {destination} ({weight} kg) ist jetzt bei "
                  "{airline} gebucht, zu {price} {currency} {scope}.\n\nUnsere Referenz für Sie ist {reference}, "
                  "die der Airline {airline_reference}. Ich melde mich, sobald {airline} bestätigt.",
        "booked_changed": "Danke für Ihre Zusage. Ihre Sendung von {origin} nach {destination} ({weight} kg) ist "
                          "jetzt bei {airline} gebucht, allerdings hat die Airline sie zu {price} {currency} {scope} "
                          "gebucht und nicht zum vereinbarten Preis. Ich kläre das mit {airline} und melde mich "
                          "bei Ihnen.\n\nUnsere Referenz für Sie ist {reference}, die der Airline "
                          "{airline_reference}.",
        "reconfirm": "Danke für Ihre Zusage. Bevor ich buche: {airline} hat den Preis für Ihre Sendung von "
                     "{origin} nach {destination} ({weight} kg) geändert. Er liegt jetzt bei {new_price} "
                     "{new_currency} {scope} statt der angebotenen {old_price} {old_currency}.\n\nSoll ich zum "
                     "neuen Preis buchen? Ein kurzes Ja genügt, dann buche ich sofort. Passt es Ihnen nicht mehr, "
                     "sagen Sie einfach Bescheid, dann buche ich nichts.",
        "cancelled": "Verstanden, ich buche Ihre Sendung von {origin} nach {destination} ({weight} kg) nicht zum "
                     "neuen Preis, und bei {airline} ist nichts gebucht.\n\nWenn Sie später ein neues Angebot "
                     "möchten, antworten Sie einfach auf diese Mail.",
        "confirmed": "Gute Nachricht: {airline} hat Ihre Buchung {airline_reference} von {origin} nach "
                     "{destination} ({weight} kg) bestätigt.\n\nIhre Referenz bei uns bleibt {reference}. Wenn "
                     "Sie wissen möchten, wo Ihre Sendung gerade ist, fragen Sie einfach.",
    },
}  # fmt: skip
# The quote's voice carries on into its booking mail: a thank-you a person adds, once per thread.
THANKS = {
    "en": " Thanks again, great to have you on board! 😊",
    "de": " Nochmals danke, schön, dass Sie mit uns fliegen! 😊",
}


def _lang(quote: Quote) -> str:
    return compose.language_of(quote.rfq)


async def _tell_customer(
    ctx: Context, quote: Quote, reply_to: int | None, what: str, notice_key: str | None = None, **facts: str
) -> None:
    """A short mail in the customer's language, in the quote's thread, signed by the same person (R24, R31).
    Numbers are formatted by code; no model call, so the quote's spend stays one call. `notice_key` marks it
    sent: one mail per key."""
    delivered = notice_key or f"booking-notice:{quote.id}:{what}"
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
        scope = (
            "für den Flughafen-zu-Flughafen-Transport ohne Abholung"
            if lang == "de"
            else "for airport-to-airport transport, excluding pickup"
        )
    text = t[what].format(scope=scope, origin=quote.option.origin, destination=quote.option.destination,
                          weight=compose.weight(quote.rfq.weight_kg, lang), **facts)  # fmt: skip
    if what == "booked" and compose.register(request) != "formal":  # once per thread; a formal mail ends calm
        text += THANKS[lang]
    body = "\n\n".join([compose.greeting(lang, name, request), text,
                        compose.signature(bye, ctx.settings.signer)])  # fmt: skip
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
