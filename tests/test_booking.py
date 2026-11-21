import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_crm import mail, quote

from pixel.agents.rfq import booking
from pixel.api.client import ApiError
from pixel.api.portal import BookingUnavailable, PortalBooking, RateExpired
from pixel.contracts import BookingStatus, QuoteStatus
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
    return [r["kind"] for r in ctx.db.execute("SELECT kind FROM events ORDER BY id")]


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


@pytest.mark.parametrize("status", [404, 409])
async def test_unknown_or_taken_rate_fails(pipe, accepted, status):
    pipe.ctx.portal = Portal(ApiError(status, "no"))
    b = await booking.book(pipe.ctx, "Q-1")
    assert b.status == BookingStatus.FAILED and kinds(pipe.ctx)[-1] == "booking.failed" and pipe.sent == []


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


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Yes, please go ahead and book.", "accept"),
        ("Bitte buchen Sie die Sendung.", "accept"),
        ("Thanks, but too expensive for us.", "decline"),
        ("We cannot accept this price.", "decline"),
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
    assert told["body"].startswith("Guten Tag,") and "370.74 EUR all-in" in told["body"]
    assert "Viele Grüße" in told["body"] and "Decimal" not in told["body"]


async def test_a_dry_run_never_books_at_the_portal(pipe, accepted):
    pipe.ctx.settings = dataclasses.replace(pipe.ctx.settings, dry_run=True)
    pipe.ctx.portal = Portal(None)
    await booking.handle_reply(pipe.ctx, accepted)
    assert pipe.ctx.portal.calls == []
