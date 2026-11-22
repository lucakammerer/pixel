import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_crm import mail, quote

from pixel.agents.rfq import booking
from pixel.api.client import ApiError
from pixel.api.portal import (
    BookingUnavailable,
    NotOnPortal,
    PortalBooking,
    RateExpired,
    RateLimited,
    RatesUnavailable,
)
from pixel.contracts import Attachment, BookingStatus, QuoteStatus
from pixel.core import poller
from pixel.crm import bookings, quotes, shipments

pytest_plugins = ["test_crm"]  # the `pipe` fixture


class Portal:
    """Answers `book` from a script: each item is a PortalBooking to return or an exception to raise."""

    def __init__(self, *script):
        self.script, self.calls, self.rates = list(script), [], 0

    async def book(self, rate_id, team_reference):
        self.calls.append((rate_id, team_reference))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return PortalBooking(booking_reference="BK-AB12CD34", team_reference=team_reference, rate_id=rate_id,
                             airline="Kestrel Airways", price=Decimal("370.74"), currency="EUR", status="booked")  # fmt: skip

    async def rate(self, origin, destination, airline, weight_kg, volume_cbm):
        self.rates += 1
        return quote().option.model_copy(update={"rate_id": "R-fresh000001"})


def kinds(ctx):
    return [r["kind"] for r in ctx.db.execute("SELECT kind FROM events WHERE kind != 'followup.read' ORDER BY id")]


@pytest.fixture
def accepted(pipe):
    quotes.save(pipe.ctx, quote(1))
    return mail(20, 1, subject="Re: Your quote", body="Looks good, please book it.\n\n> Your quote ...")


async def test_acceptance_books_once_and_tells_customer(pipe, accepted):
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    await booking.handle_reply(pipe.ctx, accepted)  # a second "yes" books nothing twice

    ((rate_id, team_reference),) = pipe.ctx.portal.calls
    shipment = shipments.get(pipe.ctx, team_reference)
    assert rate_id == "R-abcdefgh12" and shipment.status == "booked" and shipment.booking_reference == "BK-AB12CD34"
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.ACCEPTED
    assert kinds(pipe.ctx) == ["customer.accepted", "booking.requested", "booking.booked"]
    (told,) = pipe.sent
    assert told["to"] == "buyer@acme.example" and told["in_reply_to"] == 20 and team_reference in told["body"]


async def test_lost_connection_retries_and_gets_the_same_booking(pipe, accepted):
    pipe.ctx.portal = Portal(httpx.ConnectError("reset"), None)
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.status == BookingStatus.BOOKED
    assert pipe.ctx.portal.calls[0] == pipe.ctx.portal.calls[1]


async def test_expired_rate_books_a_fresh_one(pipe, accepted):
    pipe.ctx.portal = Portal(RateExpired(), None)
    b = await booking.book(pipe.ctx, "Q-1")
    assert pipe.ctx.portal.rates == 1
    assert [c[0] for c in pipe.ctx.portal.calls] == ["R-abcdefgh12", "R-fresh000001"]
    assert pipe.ctx.portal.calls[0][1] == pipe.ctx.portal.calls[1][1]  # same shipment, same team_reference
    assert b.status == BookingStatus.BOOKED and b.rate_id == "R-fresh000001"


async def test_portal_down_books_by_mail_to_the_address_given(pipe, accepted):
    pipe.ctx.portal = Portal(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.via == "email" and b.status == BookingStatus.REQUESTED
    (sent,) = pipe.sent
    assert sent["to"] == "bookings@kestrel.example" and b.team_reference in sent["body"]


async def test_no_rate_id_books_by_mail_with_the_carrier_list(pipe):
    pipe.ctx.db.execute("INSERT INTO carriers VALUES ('Kestrel Airways', 'airline', 'kestrel@air.example', 'JFK')")
    quotes.save(pipe.ctx, quote(1, rate_id=None))
    pipe.ctx.portal = Portal()
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.via == "email" and pipe.sent[0]["to"] == "kestrel@air.example" and pipe.ctx.portal.calls == []


@pytest.mark.parametrize("status", ["Status: QU. Space pending.", "Confirmed for 3 of 5 pcs. Balance pending."])
async def test_pending_airline_reply_keeps_reference_until_standalone_confirmation(pipe, accepted, status):
    pipe.ctx.portal = Portal(BookingUnavailable("Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    reference = "BK-AB12CD34"
    reply = mail(31, 1, sender="bookings@kestrel.example", subject="Booking confirmation",
                 body=f"Booking reference: {reference}\nYour reference: {b.team_reference}\n{status}")  # fmt: skip
    await booking.handle_confirmation(pipe.ctx, reply)
    pending = bookings.for_session(pipe.ctx, 1)
    assert (pending.status, pending.booking_reference) == (BookingStatus.REQUESTED, reference)
    assert kinds(pipe.ctx)[-1] == "human.note" and len(pipe.sent) == 1

    confirmed = mail(32, 32, sender=reply.sender, subject=f"Booking {reference} confirmed",
                     body="Confirmed for 5 of 5 pieces.")  # fmt: skip
    assert poller.session_of(pipe.ctx, confirmed) == 1
    await booking.handle_confirmation(pipe.ctx, confirmed)
    assert bookings.for_session(pipe.ctx, 1).status == BookingStatus.CONFIRMED
    assert len(pipe.sent) == 2


@pytest.mark.parametrize("status", [404, 409])
async def test_unknown_or_taken_rate_books_a_fresh_one(pipe, accepted, status):
    # A rate id from the desk or the browser page the bookings API does not know, or one taken: a fresh rate books.
    pipe.ctx.portal = Portal(ApiError(status, "no"), None)
    b = await booking.book(pipe.ctx, "Q-1")
    assert pipe.ctx.portal.rates == 1 and b.status == BookingStatus.BOOKED and b.rate_id == "R-fresh000001"


async def test_another_portal_error_fails(pipe, accepted):
    pipe.ctx.portal = Portal(ApiError(422, "no"))
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.status == BookingStatus.FAILED and kinds(pipe.ctx)[-1] == "booking.failed" and pipe.sent == []


class RatesDown(Portal):
    """The bookings API answers, the rate API does not: what an acceptance after 15 minutes meets in a rate outage."""

    def __init__(self, *script, error):
        super().__init__(*script)
        self.error = error

    async def rate(self, origin, destination, airline, weight_kg, volume_cbm):
        self.rates += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [RatesUnavailable("Rates are unavailable. Ask ratedesk@hackloop.mail for the rate."), RateLimited(30),
     NotOnPortal("Airline not on the portal"), ApiError(404, "no rate")],
)  # fmt: skip
async def test_expired_rate_without_a_fresh_one_books_by_mail_with_the_airline(pipe, accepted, error):
    pipe.ctx.db.execute("INSERT INTO carriers VALUES ('Kestrel Airways', 'airline', 'kestrel@air.example', 'JFK')")
    pipe.ctx.portal = RatesDown(RateExpired(), error=error)
    await booking.handle_reply(pipe.ctx, accepted)  # used to raise and leave the booking requested for good
    b = bookings.for_session(pipe.ctx, 1)
    assert b.via == "email" and b.status == BookingStatus.REQUESTED
    (sent,) = pipe.sent
    assert sent["to"] == "kestrel@air.example" and b.team_reference in sent["body"]


async def test_expired_rate_without_a_fresh_one_or_an_address_fails_and_never_raises(pipe, accepted):
    pipe.ctx.portal = RatesDown(RateExpired(), error=RatesUnavailable("Ask ratedesk@hackloop.mail."))
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.status == BookingStatus.FAILED and pipe.sent == []  # never a booking mail to the rate desk


async def test_confirmation_marks_confirmed_and_tells_once(pipe, accepted):
    pipe.ctx.portal = Portal(None)
    b = await booking.book(pipe.ctx, "Q-1")
    pipe.sent.clear()
    confirm = mail(30, 77, sender="ops@kestrel.example", subject="Booking BK-AB12CD34 confirmed")
    await booking.handle_confirmation(pipe.ctx, confirm)
    await booking.handle_confirmation(pipe.ctx, confirm)
    assert bookings.get(pipe.ctx, b.id).status == BookingStatus.CONFIRMED
    assert shipments.get(pipe.ctx, b.team_reference).status == "confirmed"
    assert len(pipe.sent) == 1 and "BK-AB12CD34" in pipe.sent[0]["body"]


async def test_confirmation_of_a_booking_by_mail_found_by_our_reference(pipe, accepted):
    # Round 27: booked by mail, so no airline reference was stored; the confirmation came in a thread of its own.
    pipe.ctx.portal = Portal(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    pipe.sent.clear()
    body = f"Hello,\n\nyour booking BK-43VLF40X is confirmed.\n\nBooking reference: BK-43VLF40X\nYour reference: {b.team_reference}\n"
    await booking.handle_confirmation(
        pipe.ctx, mail(31, 31, sender="bookings@kestrel.example", subject="Booking BK-43VLF40X confirmed", body=body)
    )
    done = bookings.get(pipe.ctx, b.id)
    assert done.status == BookingStatus.CONFIRMED and done.booking_reference == "BK-43VLF40X"
    assert len(pipe.sent) == 1 and "BK-43VLF40X" in pipe.sent[0]["body"]


async def test_the_airlines_reply_to_our_booking_by_mail_stores_its_reference(pipe, accepted):
    # Round 27: the reply in our thread names the airline's reference only as "Booking reference:".
    pipe.ctx.portal = Portal(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    body = f"Booking reference: BK-43VLF40X\nYour reference: {b.team_reference}\nStatus: booked\n"
    reply = mail(31, 1, sender="bookings@kestrel.example", subject=f"Re: Booking request {b.team_reference}", body=body)
    await booking.handle_confirmation(pipe.ctx, reply)
    assert bookings.get(pipe.ctx, b.id).booking_reference == "BK-43VLF40X"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Yes, please go ahead and book.", "accept"),
        ("Let's go with this offer.", "accept"),
        ("D'accord, merci de réserver.", "accept"),
        ("We found a cheaper option elsewhere, thanks.", "decline"),
        ("Thank you, but we have decided against it.", "decline"),
        ("Merci, mais c'est trop cher.", "decline"),
        ("If we decide to go with this offer, we will let you know.", "other"),
        ("Bitte buchen Sie die Sendung.", "accept"),
        ("Thanks, but too expensive for us.", "decline"),
        ("We cannot accept this price.", "decline"),
        # rounds 15 and 16: both were booked at the portal, "go ahead" read as the acceptance
        ("Thank you for your offer. That seems too low to be right, so we will not go ahead with it.", "decline"),
        ("Thank you for your offer, but your price is too high for us, so we will not go ahead with it.", "decline"),
        ("Does the price include pickup?", "other"),
    ],
)
def test_intent(text, said):
    assert booking.intent(text) == said


async def test_decline_and_question(pipe, accepted):
    await booking.handle_reply(pipe.ctx, mail(21, 1, body="Is the transit time guaranteed?"))
    await booking.handle_reply(pipe.ctx, mail(22, 1, body="No thanks, we went with another offer."))
    assert kinds(pipe.ctx) == ["human.note", "customer.declined"]
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.DECLINED and pipe.sent == []


@pytest.mark.parametrize(("name", "said"), [("customer-accepts.json", "accept"), ("customer-thanks.json", "other")])
def test_intent_of_the_captured_customer_replies(name, said):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / name).read_text())
    assert booking.intent(f"{raw['subject']}\n{raw['body']}") == said


async def test_german_customer_gets_a_german_mail_with_the_total_written_once(pipe):
    german = quote(1)
    german = german.model_copy(update={"rfq": german.rfq.model_copy(update={"language": "de"})})
    quotes.save(pipe.ctx, german)
    pipe.ctx.portal = Portal(None)
    await booking.book(pipe.ctx, "Q-1", reply_to=20)
    (told,) = pipe.sent
    assert told["body"].startswith("Guten Tag") and "370.74 EUR all-in" in told["body"]
    assert "Viele Grüße" in told["body"] and "Decimal" not in told["body"]


async def test_the_booking_mail_greets_and_signs_as_the_quote_did(pipe, accepted):
    """The quote said "Hi Klaus … Cheers", the booking mail "Hello," and "Best regards": two writers in one thread."""
    pipe.ctx.db.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (1, 1, 'in', 'buyer@acme.example', 'us@pixel', 'RFQ', ?, 't', '{}')",
        ("Hi there,\n\nplease quote FRA to JFK.\n\nCheers,\nKlaus Example\nExample GmbH",),
    )
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    (told,) = pipe.sent
    # Same greeting and sign-off as the quote of this session.
    assert told["body"].startswith("Hi Klaus,\n\n")
    assert "\n\nCheers,\nLena Berger\n" in told["body"] and "great to have you on board" in told["body"]


async def test_the_booking_mail_to_a_formal_customer_stays_formal(pipe, accepted):
    pipe.ctx.db.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (1, 1, 'in', 'buyer@acme.example', 'us@pixel', 'RFQ', ?, 't', '{}')",
        ("Dear Sir or Madam,\n\nplease quote FRA to JFK.\n\nKind regards\nJonas Example\nExample GmbH",),
    )
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    (told,) = pipe.sent
    assert told["body"].startswith("Dear Jonas Example,\n\n") and "\n\nKind regards,\nLena Berger\n" in told["body"]
    assert "😊" not in told["body"] and "on board" not in told["body"]


async def test_a_dry_run_never_books_at_the_portal(pipe, accepted):
    pipe.ctx.settings = dataclasses.replace(pipe.ctx.settings, dry_run=True)
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    assert pipe.ctx.portal.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "We will not go ahead.",
        "We can't accept.",
        "Please do not book.",
        "We won't proceed.",
        "Don't confirm.",
        "We are unable to take it.",
        "We would rather not move forward.",
        "We won’t be moving forward.",
        "Too low.",
        "Too high.",
        "Not competitive.",
        "We will pass on this.",
        "Bitte nicht buchen.",
        "Wir können das Angebot nicht annehmen.",
        "Wir möchten nicht fortfahren.",
        "Leider nicht.",
    ],
)
def test_decline_phrases(text):
    assert booking.intent(text) == "decline"


async def test_captured_decline_never_books(pipe):
    path = Path(__file__).parent / "fixtures/mails/customer-declines-too-low.json"
    raw = json.loads(path.read_text(encoding="utf8"))
    session_id = raw["references"][0]
    sent_quote = quote(session_id, customer=raw["from"]).model_copy(update={"sent_message_id": raw["in_reply_to"]})
    quotes.save(pipe.ctx, sent_quote)
    pipe.ctx.portal = Portal()
    reply = mail(
        raw["id"],
        session_id,
        sender=raw["from"],
        to=raw["to"],
        subject=raw["subject"],
        body=raw["body"],
        in_reply_to=raw["in_reply_to"],
    )
    await booking.handle_reply(pipe.ctx, reply)
    assert quotes.get(pipe.ctx, sent_quote.id).status == QuoteStatus.DECLINED
    assert kinds(pipe.ctx) == ["customer.declined"]
    assert bookings.for_session(pipe.ctx, session_id) is None
    assert pipe.ctx.portal.calls == [] and pipe.sent == []


@pytest.mark.parametrize("body", ["Please book it.", "No thanks."])
async def test_unsent_draft_reply_needs_a_person(pipe, body):
    draft = quote(status=QuoteStatus.DRAFT)
    quotes.save(pipe.ctx, draft)
    pipe.ctx.portal = Portal()
    await booking.handle_reply(pipe.ctx, mail(20, draft.session_id, body=body))
    assert quotes.get(pipe.ctx, draft.id).status == QuoteStatus.DRAFT
    assert kinds(pipe.ctx) == ["human.note"]
    assert pipe.ctx.portal.calls == [] and pipe.sent == []


@pytest.mark.parametrize(
    "text",
    [
        "Could you confirm whether pickup is included?",
        "Please confirm the price.",
        "Can you tell me when to book it?",
        "We haven't accepted the offer.",
        "If pickup is included, please book it.",
        "We accept that you need to check the price.",
        "Please book a call to discuss.",
        "Are we agreed?",
        "Können Sie bitte buchen, wenn die Freigabe vorliegt?",
    ],
)
async def test_question_or_conditional_never_books(pipe, accepted, text):
    pipe.ctx.portal = Portal()
    await booking.handle_reply(pipe.ctx, mail(21, 1, subject="Re: Please book it", body=text))
    assert pipe.ctx.portal.calls == []
    assert not bookings.for_session(pipe.ctx, 1)


async def test_customer_notice_preserves_quote_total_and_currency(pipe, accepted):
    q = quotes.for_session(pipe.ctx, 1)
    option = q.option.model_copy(update={"total": Decimal("450.74"), "pickup": Decimal(80), "trucker": "Truck"})
    quotes.save(pipe.ctx, q.model_copy(update={"total": option.total, "option": option}))
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    assert bookings.for_session(pipe.ctx, 1).price == Decimal("370.74")
    assert "450.74 EUR all-in" in pipe.sent[0]["body"]


@pytest.mark.parametrize(("language", "scope"), [("en", "excluding pickup"), ("de", "ohne Abholung")])
async def test_customer_notice_identifies_partial_door_quote(pipe, accepted, language, scope):
    q = quotes.for_session(pipe.ctx, 1)
    quotes.save(
        pipe.ctx, q.model_copy(update={"rfq": q.rfq.model_copy(update={"door_pickup": True, "language": language})})
    )
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    assert scope in pipe.sent[0]["body"]


async def test_refresh_preserves_cheaper_billed_break(pipe, accepted):
    q = quotes.for_session(pipe.ctx, 1)
    option = q.option.model_copy(update={"billed_kg": q.option.chargeable_kg + 100})
    quotes.save(pipe.ctx, q.model_copy(update={"option": option}))
    pipe.ctx.portal = Portal(RateExpired(), None)
    from unittest.mock import AsyncMock

    pipe.ctx.portal.rate = AsyncMock(wraps=pipe.ctx.portal.rate)
    await booking.handle_reply(pipe.ctx, accepted)
    assert pipe.ctx.portal.rate.await_args.args[-2:] == (option.billed_kg, Decimal(0))


async def test_reentry_after_interrupted_acceptance_reuses_team_reference(pipe, accepted):
    import asyncio
    import contextlib

    class InterruptedPortal(Portal):
        async def book(self, rate_id, team_reference):
            self.calls.append((rate_id, team_reference))
            await asyncio.Future()

    pipe.ctx.portal = InterruptedPortal()
    pending = asyncio.create_task(booking.handle_reply(pipe.ctx, accepted))
    await asyncio.sleep(0)
    assert pipe.ctx.portal.calls
    first = pipe.ctx.portal.calls[0]
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    assert pipe.ctx.portal.calls == [first] and len(pipe.sent) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Looks good, please book it and pass on the AWB number.",
        "Please go ahead. Leider nicht vor Montag abholbereit.",
        "Price is fine, not too high. Book it.",
    ],
)
def test_an_acceptance_that_names_a_decline_word_still_books(text):
    assert booking.intent(text) == "accept"


@pytest.mark.parametrize(
    "text",
    [
        "Bitte buchen Sie.",
        "Gerne buchen.",
        "Hiermit erteilen wir Ihnen den Auftrag.",
        "We accept the quote for the shipment.",
        "Please book the shipment as quoted.",
        "Please book it and send us the AWB once available.",
        "Looks good - please go ahead.",
        "Please confirm the booking.",
        "Could you book it, please?",
    ],
)
def test_a_plain_acceptance_books(text):
    assert booking.intent(text) == "accept"  # since #76 each was "other": no booking, no tracking bonus


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for your offer, but we have decided not to go ahead.",
        "We are not going to go ahead with this.",
        "Thanks, but we'd prefer not to book it.",
        "No need to book it.",
        "Bitte buchen Sie nicht.",
        "Bitte noch nichts buchen.",
        "Wir sind nicht einverstanden.",
        "Wir haben den Auftrag anderweitig vergeben.",
        "This is not what we agreed.",
        "As agreed, please hold the shipment until further notice.",
        "We decided to go ahead with a competitor.",
        "Please go ahead once we confirm pickup.",
        "Bitte buchen Sie erst nach Freigabe.",
        "Auftragsnummer: 4500123",  # a packing list's order number
        "We accept no liability for delays.",  # an attached document's terms
    ],
)
def test_a_negated_or_held_acceptance_never_books(text):
    assert booking.intent(text) != "accept"  # rounds 9, 10 and 15 were booked on a decline


@pytest.mark.parametrize(
    "text",
    [
        "Not too expensive, please book.",
        "We would like to book.",
        "We'd like to proceed with your offer.",
        "Please book it and don't forget the AWB.",
        "Sie können buchen.",
        "Angebot angenommen.",
    ],
)
def test_more_plain_acceptances_book(text):
    assert booking.intent(text) == "accept"


async def test_an_acceptance_written_below_the_quoted_mail_books(pipe):
    quotes.save(pipe.ctx, quote(1))
    pipe.ctx.portal = Portal(None)
    body = "> If that works for you, just reply and I'll book it right away.\n> Lena\n\nPlease book it.\n\nSara"
    await booking.handle_reply(pipe.ctx, mail(21, 1, subject="Re: Your quote", body=body))
    assert pipe.ctx.portal.calls, kinds(pipe.ctx)


async def test_an_acceptance_that_restates_the_cargo_at_the_priced_weight_books(pipe):
    body = "Go ahead and book it, gross weight 150 kg and 0.4 cbm as sent."
    quotes.save(pipe.ctx, quote(1))
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, mail(23, 1, subject="Re: Your quote", body=body))
    assert pipe.ctx.portal.calls and [s["purpose"] for s in pipe.sent] != ["quote"], kinds(pipe.ctx)


async def test_a_decline_word_after_the_booking_keeps_the_quote_accepted(pipe, accepted):
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    body = "Thanks! Please cancel our pickup slot request with your colleague."
    await booking.handle_reply(pipe.ctx, mail(22, 1, subject="Re: Booked", body=body))
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.ACCEPTED


async def test_a_chinese_thread_is_told_in_chinese_and_never_loses_a_reference(pipe, monkeypatch):
    """The thread keeps the request's language after the quote, also when the customer accepts in English; a
    translation that drops a reference or the price is not sent, the English notice is."""
    from pixel import llm
    from pixel.contracts import LlmResult

    zh = quote(1)
    zh = zh.model_copy(update={"rfq": zh.rfq.model_copy(update={"language": "zh"})})
    replies = iter([lambda english: "您好，\n\n" + english.split("\n\n", 1)[1], lambda english: "您好，预订已确认。"])

    async def fake_ask(ctx, **kw):
        assert kw["purpose"] == "booking.translate" and "zh" in kw["system"]
        body = next(replies)(kw["user"])
        return LlmResult(data={"body": body}, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    facts = {"airline": "Kestrel Airways", "reference": "PX-ABC123", "airline_reference": "BK-AB12CD34"}
    await booking._tell_customer(pipe.ctx, zh, 20, "booked", price="370.74", currency="EUR", **facts)
    await booking._tell_customer(pipe.ctx, zh, 20, "confirmed", **facts)
    booked, confirmed = (m["body"] for m in pipe.sent)
    assert booked.startswith("您好，") and "PX-ABC123" in booked and "370.74 EUR" in booked
    assert confirmed.startswith("Hello") and "PX-ABC123" in confirmed and "BK-AB12CD34" in confirmed


PACKLISTE = b"Packliste\nAuftragsnummer: 4500123\nKolli: 2\nBruttogewicht: 150 kg\n"


class Files:
    async def attachment(self, message_id, attachment_id):
        return PACKLISTE


async def test_a_packing_list_in_a_reply_is_no_acceptance(pipe):
    quotes.save(pipe.ctx, quote(1))
    pipe.ctx.portal = Portal(None)
    pipe.ctx.mailbox = Files()
    reply = mail(20, 1, subject="Re: Your quote", body="Anbei die aktualisierte Packliste. Bleibt der Preis gleich?")
    reply = reply.model_copy(update={"attachments": (Attachment(id=1, filename="packliste.txt",
                                                                content_type="text/plain", size=60),)})  # fmt: skip
    await booking.handle_reply(pipe.ctx, reply)
    assert pipe.ctx.portal.calls == []
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.SENT
    assert "customer.accepted" not in kinds(pipe.ctx)


async def test_changed_cargo_after_the_quote_is_requoted_in_reply_to_the_change(pipe, monkeypatch):
    # Round 86: "the shipment details have changed to: 5 pieces, 1317 kg gross weight, 8.18 cbm. Could you please
    # update your offer?" went to a person; the scorer scores our answer to that mail and saw none.
    from pixel.agents.rfq import agent
    from pixel.contracts import Message
    from pixel.pricing import engine

    raw = json.loads((Path(__file__).parent / "fixtures/mails/reply-changed-cargo-update-offer.json").read_text())
    change = Message(id=raw["id"], session_id=1, direction="in", sender="buyer@acme.example", to=raw["to"],
                     subject=raw["subject"], body=raw["body"], in_reply_to=501, sent_at=raw["received_at"])  # fmt: skip
    first = quote(1)
    first = first.model_copy(update={"rfq": first.rfq.model_copy(update={"weight_kg": Decimal(753),
                                                                         "volume_cbm": Decimal("4.87")})})  # fmt: skip
    quotes.save(pipe.ctx, first)
    seen = {}

    async def options(ctx, rfq, deadline):
        seen["rfq"] = rfq
        new = first.option.model_copy(update={"chargeable_kg": Decimal("1366.06"), "total": Decimal("4100.00"),
                                              "total_eur": Decimal("4100.00"), "rate_id": "R-new"})  # fmt: skip
        pipe.emit(ctx, "option.found", "pricing", session_id=1, payload={"option": new.model_dump(mode="json")})
        return [new, first.option]

    monkeypatch.setattr(engine, "options", options)
    monkeypatch.setattr(agent, "_check", lambda *a: None)
    await booking.handle_reply(pipe.ctx, change)

    assert (seen["rfq"].weight_kg, seen["rfq"].volume_cbm, seen["rfq"].pieces) == (Decimal(1317), Decimal("8.18"), 5)
    assert [s["purpose"] for s in pipe.sent] == ["quote"] and pipe.quoted == [(24612, Decimal("4100.00"))]
    assert "human.note" not in kinds(pipe.ctx)


@pytest.mark.parametrize(
    ("text", "books"),
    [
        ("If the all-in stays below EUR 400, please book it.", True),  # 370.74 < 400
        ("Falls der Preis unter 3.200 € bleibt, bitte direkt buchen.", True),
        ("Book it if it's up to 370.74 EUR.", True),  # "up to" includes the cap
        ("If the all-in stays below EUR 370.74, please book it.", False),  # "below" does not
        ("If the all-in is below EUR 300, please book it.", False),
        ("Falls der Preis unter 300 € bleibt, bitte direkt buchen.", False),  # used to book: "3." split the sentence
        ("If it stays below USD 400, please book it.", False),  # another currency: a person converts
        ("If you can guarantee departure on Monday, please book.", False),
    ],
)
async def test_a_conditional_acceptance_books_only_when_our_total_meets_its_cap(pipe, accepted, text, books):
    # It used to be read as a thank-you after the quote and dropped without a note.
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Your quote", body=text, in_reply_to=501))
    assert bool(pipe.ctx.portal.calls) is books
    if not books:
        assert kinds(pipe.ctx)[-1] == "human.note" and pipe.sent == []


class Attached:
    def __init__(self, text):
        self.text = text

    async def attachment(self, message_id, attachment_id):
        return self.text.encode()


@pytest.mark.parametrize(
    ("attached", "status", "told"),
    [("Booking reference: BK-43VLF40X\nStatus: booked", BookingStatus.CONFIRMED, 1),
     ("Booking request PX: could not be confirmed, no space this week.", BookingStatus.FAILED, 0)],
)  # fmt: skip
async def test_the_airlines_answer_only_in_an_attachment_confirms_or_fails(pipe, accepted, attached, status, told):
    pipe.ctx.portal = Portal(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    pipe.sent.clear()
    pipe.ctx.mailbox = Attached(attached.replace("PX", b.team_reference))
    reply = mail(31, 1, sender="bookings@kestrel.example", subject=f"Re: Booking request {b.team_reference}",
                 body="Hello,\n\nplease find our answer attached.\n\nKind regards")  # fmt: skip
    reply = reply.model_copy(update={"attachments": (Attachment(id=1, filename="answer.txt",
                                                                content_type="text/plain", size=60),)})  # fmt: skip
    await booking.handle_confirmation(pipe.ctx, reply)
    assert bookings.get(pipe.ctx, b.id).status == status and len(pipe.sent) == told


class Cancels(Portal):
    def __init__(self, *script, cancel_error=None):
        super().__init__(*script)
        self.cancel_error, self.cancelled = cancel_error, []

    async def cancel(self, booking_reference):
        self.cancelled.append(booking_reference)
        if self.cancel_error:
            raise self.cancel_error


CANCEL = "Please cancel the booking, the order was withdrawn."


async def test_a_cancellation_after_the_booking_cancels_at_the_portal_and_tells_the_customer(pipe, accepted):
    pipe.ctx.portal = Cancels(None)
    await booking.handle_reply(pipe.ctx, accepted)
    pipe.sent.clear()
    await booking.handle_reply(pipe.ctx, mail(40, 1, subject="Re: Booked", body=CANCEL, in_reply_to=501))
    b = bookings.for_session(pipe.ctx, 1)
    assert pipe.ctx.portal.cancelled == ["BK-AB12CD34"] and b.status == BookingStatus.CANCELLED
    assert shipments.get(pipe.ctx, b.team_reference).status == "cancelled"
    assert [s["to"] for s in pipe.sent] == ["buyer@acme.example"]


@pytest.mark.parametrize("portal_error", [ApiError(503, "down"), ApiError(404, "unknown"), httpx.ConnectError("x")])
async def test_a_cancellation_the_portal_refuses_is_sent_to_the_airline(pipe, accepted, portal_error):
    pipe.ctx.db.execute("INSERT INTO carriers VALUES ('Kestrel Airways', 'airline', 'kestrel@air.example', 'JFK')")
    pipe.ctx.portal = Cancels(None, cancel_error=portal_error)
    await booking.handle_reply(pipe.ctx, accepted)
    pipe.sent.clear()
    await booking.handle_reply(pipe.ctx, mail(40, 1, subject="Re: Booked", body=CANCEL, in_reply_to=501))
    b = bookings.for_session(pipe.ctx, 1)
    assert b.status == BookingStatus.CANCELLED  # used to stay booked with a note, the customer never heard
    airline, customer = pipe.sent
    assert airline["to"] == "kestrel@air.example" and b.team_reference in airline["body"]
    assert customer["to"] == "buyer@acme.example"


async def test_a_cancellation_of_a_booking_by_mail_without_an_airline_reference_goes_to_the_airline(pipe, accepted):
    pipe.ctx.portal = Cancels(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    pipe.ctx.db.execute("INSERT INTO carriers VALUES ('Kestrel Airways', 'airline', 'bookings@kestrel.example', 'JFK')")
    await booking.handle_reply(pipe.ctx, accepted)
    pipe.sent.clear()
    await booking.handle_reply(pipe.ctx, mail(40, 1, subject="Re: Your quote", body=CANCEL, in_reply_to=501))
    assert pipe.ctx.portal.cancelled == []
    assert [s["to"] for s in pipe.sent] == ["bookings@kestrel.example", "buyer@acme.example"]


async def test_a_cancellation_with_no_way_to_the_airline_goes_to_a_person(pipe, accepted):
    pipe.ctx.portal = Cancels(None, cancel_error=ApiError(503, "down"))
    await booking.handle_reply(pipe.ctx, accepted)
    pipe.sent.clear()
    await booking.handle_reply(pipe.ctx, mail(40, 1, subject="Re: Booked", body=CANCEL, in_reply_to=501))
    assert bookings.for_session(pipe.ctx, 1).status == BookingStatus.BOOKED and pipe.sent == []
    assert kinds(pipe.ctx)[-1] == "human.note"


async def test_an_airline_answer_with_an_attachment_that_confirms_nothing_confirms_nothing(pipe, accepted):
    # Since #332 every answer with an attachment in our booking thread reaches handle_confirmation.
    pipe.ctx.portal = Portal(BookingUnavailable("Booking is down. Book by email with bookings@kestrel.example."))
    b = await booking.book(pipe.ctx, "Q-1")
    pipe.sent.clear()
    pipe.ctx.mailbox = Attached("Dangerous goods declaration form\nShipper:\nUN number:")
    reply = mail(31, 1, sender="bookings@kestrel.example", subject=f"Re: Booking request {b.team_reference}",
                 body="Hello, we received your request, please fill in the attached form.")  # fmt: skip
    reply = reply.model_copy(update={"attachments": (Attachment(id=1, filename="form.txt",
                                                                content_type="text/plain", size=9),)})  # fmt: skip
    await booking.handle_confirmation(pipe.ctx, reply)
    assert bookings.get(pipe.ctx, b.id).status == BookingStatus.REQUESTED and pipe.sent == []
    assert kinds(pipe.ctx)[-1] == "human.note"


@pytest.mark.parametrize(
    "text",
    ["Bitte buchen Sie die Sendung. Wenn die Buchung bestätigt ist, schicken Sie uns die Referenz.",
     "Please go ahead. If possible, book the earliest flight."],
)  # fmt: skip
async def test_an_acceptance_beside_a_condition_without_a_cap_still_books(pipe, accepted, text):
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Your quote", body=text, in_reply_to=501))
    assert pipe.ctx.portal.calls, kinds(pipe.ctx)
