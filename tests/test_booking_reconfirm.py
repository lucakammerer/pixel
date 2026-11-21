"""A booking price that moved after the quote: the customer confirms the new price before we book it (R22, R24)."""

import json
from datetime import date
from decimal import Decimal as D

import httpx
import pytest
from test_crm import mail, quote

from pixel.agents.rfq import booking
from pixel.api.portal import PortalBooking, RateExpired, RateLimited, RatesUnavailable
from pixel.contracts import BookingStatus, LaneTariff, QuoteStatus, Source
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import store

pytest_plugins = ["test_crm"]  # the `pipe` fixture


class Portal:
    """Rates from `prices` (the last one repeats; an exception is raised), bookings from `script` (an exception is
    raised, anything else books). The airline books at the price of the rate booked, or at `booked_at`."""

    def __init__(self, *script, prices=("370.74",), booked_at=None):
        self.script, self.prices, self.booked_at = list(script), list(prices), booked_at
        self.calls, self.rates, self.issued = [], [], {}

    async def rate(self, origin, destination, airline, weight_kg, volume_cbm, not_after=None):
        price = self.prices.pop(0) if len(self.prices) > 1 else self.prices[0]
        if isinstance(price, Exception):
            raise price
        rate_id = f"R-fresh{len(self.rates) + 1:06d}"
        self.rates.append(rate_id)
        self.issued[rate_id] = D(price)
        return quote().option.model_copy(
            update={"rate_id": rate_id, "freight": D(price), "total": D(price), "total_eur": D(price)}
        )

    async def book(self, rate_id, team_reference):
        self.calls.append((rate_id, team_reference))
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        price = D(self.booked_at) if self.booked_at else self.issued.get(rate_id, D("370.74"))
        return PortalBooking(booking_reference="BK-AB12CD34", team_reference=team_reference, rate_id=rate_id,
                             airline="Kestrel Airways", price=price, currency="EUR", status="booked")  # fmt: skip


def kinds(ctx):
    return [r["kind"] for r in ctx.db.execute("SELECT kind FROM events ORDER BY id")]


def payloads(ctx, kind):
    rows = ctx.db.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [json.loads(r["payload"]) for r in rows]


def state(ctx):
    raw = ctx.db.execute("SELECT value FROM kv WHERE key = 'booking-reconfirm:Q-1'").fetchone()
    return json.loads(raw["value"]) if raw else None


ACCEPT = mail(20, 1, subject="Re: Your quote", body="Looks good, please book it.\n\n> Your quote ...")


def reply(id, body):
    return mail(id, 1, subject="Re: Your quote", body=body)


@pytest.fixture
def quoted(pipe):
    quotes.save(pipe.ctx, quote(1))
    return pipe


@pytest.mark.parametrize(
    ("language", "words"),
    [("en", ("Shall I book it at the new price?", "instead of the 450.74 EUR")),
     ("de", ("Soll ich zum neuen Preis buchen?", "statt der angebotenen 450.74 EUR"))],
)  # fmt: skip
async def test_a_price_rise_before_booking_asks_the_customer_first(pipe, language, words):
    q = quote(1)
    option = q.option.model_copy(update={"total": D("450.74"), "pickup": D(80), "trucker": "Truck"})
    rfq = q.rfq.model_copy(update={"language": language})
    quotes.save(pipe.ctx, q.model_copy(update={"total": option.total, "option": option, "rfq": rfq}))
    pipe.ctx.portal = Portal(prices=("407.81",))  # +10 % on the airline part
    await booking.handle_reply(pipe.ctx, ACCEPT)

    assert pipe.ctx.portal.calls == [] and bookings.for_session(pipe.ctx, 1) is None
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.SENT
    assert kinds(pipe.ctx) == ["option.found", "human.note"]
    (asked,) = pipe.sent
    assert "487.81 EUR" in asked["body"] and all(w in asked["body"] for w in words)  # the new air price plus pickup
    assert asked["in_reply_to"] == 20 and "😊" not in asked["body"]
    assert state(pipe.ctx)["status"] == "pending" and state(pipe.ctx)["total"] == "487.81"


async def test_a_confirmed_new_price_books_the_new_rate_and_states_the_new_total(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=("407.81",))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    await booking.handle_reply(pipe.ctx, reply(21, "Yes, please."))

    ((rate_id, team_reference),) = pipe.ctx.portal.calls
    assert rate_id == "R-fresh000002"  # read again when the customer said yes, still 407.81
    done = bookings.for_session(pipe.ctx, 1)
    assert done.status == BookingStatus.BOOKED and done.price == D("407.81")
    assert shipments.get(pipe.ctx, team_reference).status == "booked"
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.ACCEPTED
    assert payloads(pipe.ctx, "customer.accepted")[0]["after_price_change"] is True
    _, told = pipe.sent
    assert "407.81 EUR all-in" in told["body"] and "370.74" not in told["body"] and told["in_reply_to"] == 21
    assert state(pipe.ctx)["status"] == "confirmed"


async def test_a_declined_new_price_books_nothing(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=("407.81",))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    await booking.handle_reply(pipe.ctx, reply(21, "No, thanks."))

    assert pipe.ctx.portal.calls == [] and bookings.for_session(pipe.ctx, 1) is None
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.DECLINED
    assert payloads(pipe.ctx, "customer.declined") == [{"after_price_change": True}]
    _, cancelled = pipe.sent
    assert "won't book" in cancelled["body"] and cancelled["in_reply_to"] == 21
    assert state(pipe.ctx)["status"] == "declined"


async def test_a_question_about_the_new_price_keeps_it_pending(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=("407.81",))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    await booking.handle_reply(pipe.ctx, reply(21, "Does the new price include fuel?"))

    assert pipe.ctx.portal.calls == [] and len(pipe.sent) == 1
    assert payloads(pipe.ctx, "human.note")[-1]["note"] == "Customer reply to the new booking price needs a person"
    assert state(pipe.ctx)["status"] == "pending"
    await booking.handle_reply(pipe.ctx, reply(22, "Ja, das passt."))
    assert [c[0] for c in pipe.ctx.portal.calls] == ["R-fresh000002"]


async def test_replayed_replies_ask_once_and_book_once(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=("407.81",))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    await booking.handle_reply(pipe.ctx, ACCEPT)  # the acceptance delivered again: no second question
    assert len(pipe.sent) == 1 and pipe.ctx.portal.rates == ["R-fresh000001"]
    yes = reply(21, "Yes.")
    await booking.handle_reply(pipe.ctx, yes)
    await booking.handle_reply(pipe.ctx, yes)
    await booking.handle_reply(pipe.ctx, ACCEPT)

    assert len(pipe.ctx.portal.calls) == 1 and len(pipe.sent) == 2
    assert kinds(pipe.ctx).count("booking.booked") == 1


async def test_an_expired_rate_whose_fresh_price_rose_is_reconfirmed_on_the_same_shipment(quoted):
    pipe = quoted
    # The check before booking gets no answer, the rate has expired by the booking, the fresh one costs 10 % more.
    pipe.ctx.portal = Portal(RateExpired(), prices=(RateLimited(5), "407.81"))
    await booking.handle_reply(pipe.ctx, ACCEPT)

    requested = bookings.for_session(pipe.ctx, 1)
    assert requested.status == BookingStatus.REQUESTED and requested.booking_reference is None
    assert [c[0] for c in pipe.ctx.portal.calls] == ["R-abcdefgh12"] and len(pipe.sent) == 1
    await booking.handle_reply(pipe.ctx, reply(21, "Yes, please."))

    first, second = pipe.ctx.portal.calls
    assert second == ("R-fresh000002", first[1])  # same team_reference
    done = bookings.for_session(pipe.ctx, 1)
    assert done.id == requested.id and done.status == BookingStatus.BOOKED and done.price == D("407.81")
    assert "407.81 EUR all-in" in pipe.sent[-1]["body"]


async def test_a_declined_new_price_after_an_expired_rate_fails_the_unbooked_booking(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(RateExpired(), prices=(RateLimited(5), "407.81"))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    await booking.handle_reply(pipe.ctx, reply(21, "Nein."))

    b = bookings.for_session(pipe.ctx, 1)
    assert b.status == BookingStatus.FAILED and shipments.get(pipe.ctx, b.team_reference).status == "declined"
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.DECLINED
    assert len(pipe.ctx.portal.calls) == 1 and "won't book" in pipe.sent[-1]["body"]


@pytest.mark.parametrize("failure", [RateLimited(5), RatesUnavailable("down"), httpx.ConnectError("reset")])
async def test_no_answer_to_the_price_check_books_as_quoted(quoted, failure):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=(failure,))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    assert [c[0] for c in pipe.ctx.portal.calls] == ["R-abcdefgh12"]
    assert kinds(pipe.ctx) == ["customer.accepted", "booking.requested", "booking.booked"]


async def test_a_price_that_keeps_moving_goes_to_a_person(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(prices=("407.81", "448.59", "493.45", "542.80"))
    await booking.handle_reply(pipe.ctx, ACCEPT)
    for n in (21, 22, 23):
        await booking.handle_reply(pipe.ctx, reply(n, "Yes."))

    assert pipe.ctx.portal.calls == [] and len(pipe.sent) == booking.MAX_RECONFIRM_ATTEMPTS
    assert state(pipe.ctx)["status"] == "held"
    assert payloads(pipe.ctx, "human.note")[-1]["note"] == "Booking price keeps changing; a person decides"
    await booking.handle_reply(pipe.ctx, reply(24, "Yes."))
    assert pipe.ctx.portal.calls == [] and quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.SENT


async def test_a_booking_at_another_price_tells_the_customer_and_a_person(quoted):
    pipe = quoted
    pipe.ctx.portal = Portal(booked_at="380.00")
    await booking.handle_reply(pipe.ctx, ACCEPT)

    assert bookings.for_session(pipe.ctx, 1).price == D("380.00")
    assert payloads(pipe.ctx, "human.note")[-1]["booked_total"] == "380.00"
    (told,) = pipe.sent
    assert "380.00 EUR all-in" in told["body"] and "not at the price we agreed" in told["body"]


async def test_a_tariff_that_rose_before_booking_by_mail_is_reconfirmed(pipe):
    pipe.ctx.db.execute("INSERT INTO carriers VALUES ('Kestrel Airways', 'airline', 'kestrel@air.example', 'JFK')")
    q = quote(1, rate_id=None, total="300.00")
    quotes.save(pipe.ctx, q.model_copy(update={"option": q.option.model_copy(update={"source": Source.SHEET})}))
    store.upsert(pipe.ctx, [LaneTariff(
        carrier="kestrel airways", origin="FRA", destination="JFK", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v2", transit_days=3, minimum=D(0), breaks=((D(0), D("2.20")),),
        fuel_per_kg=D(0), security_per_kg=D(0), source="test",
    )])  # fmt: skip
    pipe.ctx.portal = Portal()
    await booking.handle_reply(pipe.ctx, ACCEPT)
    assert len(pipe.sent) == 1 and "330.00 EUR" in pipe.sent[0]["body"] and bookings.for_session(pipe.ctx, 1) is None

    await booking.handle_reply(pipe.ctx, reply(21, "We confirm the new price."))
    b = bookings.for_session(pipe.ctx, 1)
    assert b.via == "email" and b.price == D("330.00")
    airline = next(m for m in pipe.sent if m["to"] == "kestrel@air.example")
    assert "EUR 330.00" in airline["body"] and pipe.ctx.portal.calls == []


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Yes.", "accept"),
        ("Yes, please.", "accept"),
        ("Ja, bitte.", "accept"),
        ("Ja, das passt.", "accept"),
        ("We confirm the new price.", "accept"),
        ("Wir bestätigen den neuen Preis.", "accept"),
        ("That's fine.", "accept"),
        ("OK with the new price.", "accept"),
        ("Please proceed at the new price.", "accept"),
        ("Please book it.", "accept"),
        ("No.", "decline"),
        ("Nein.", "decline"),
        ("No, thanks.", "decline"),
        ("Nein danke.", "decline"),
        ("Not at that price.", "decline"),
        ("That's over our budget.", "decline"),
        ("Nicht zu diesem Preis.", "decline"),
        ("We cannot accept this price.", "decline"),
        ("Does the new price include fuel?", "other"),
        ("Yes?", "other"),
        ("If the price holds, yes.", "other"),
        ("Yes, but only after we check with finance.", "other"),
        ("Yesterday we sent the documents.", "other"),
    ],
)
def test_reconfirm_intent(text, said):
    assert booking.reconfirm_intent(text) == said


@pytest.mark.parametrize("text", ["Yes.", "Ja, bitte.", "That's fine."])
def test_a_bare_yes_is_no_acceptance_of_a_quote(text):
    assert booking.intent(text) == "other"  # only the answer to our price question reads it as one
