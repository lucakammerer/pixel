import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_crm import quote

from pixel import llm
from pixel.agents.rfq import booking
from pixel.agents.router.agent import classify
from pixel.api.mailbox import RATEDESK, to_message
from pixel.config import settings
from pixel.contracts import Booking, BookingStatus, MailKind
from pixel.core import poller
from pixel.core.context import Context
from pixel.core.outbox import _store
from pixel.crm import bookings, quotes

MAILS = Path(__file__).parent / "fixtures" / "mails"
RFQ = "rfq-door-no-origin.json"
EXPECTED = {
    RFQ: MailKind.CUSTOMER_RFQ,
    "bid-airline-priced.json": MailKind.CARRIER_BID,
    "bid-decline-lane.json": MailKind.CARRIER_BID,
    "customer-accepts.json": MailKind.CUSTOMER_REPLY,
    "customer-thanks.json": MailKind.CUSTOMER_REPLY,
    "booking-confirmed.json": MailKind.BOOKING_CONFIRMATION,
    "rfq-door-with-packing-list.json": MailKind.CUSTOMER_RFQ,  # a customer attaches files too
    "synthetic-tariff.json": MailKind.TARIFF,
    "synthetic-tracking.json": MailKind.TRACKING_QUESTION,
    "ratedesk-portal-rates.json": MailKind.RATEDESK_REPLY,
} | {f.name: MailKind.CUSTOMER_RFQ for f in MAILS.glob("rfq-*.json")}  # every captured request, by rules


def mail(name: str):
    return to_message(json.loads((MAILS / name).read_text()), "in")


@pytest.fixture
def ctx(conn, monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("the rules must place this mail")

    monkeypatch.setattr(llm, "ask_json", no_model)
    ctx = Context(settings(), conn, None, None, None, None)
    rfq = poller.ingest(ctx, mail(RFQ))
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (?, ?, 'quoted', ?)",
                 (rfq.id, rfq.sender, rfq.sent_at.isoformat()))  # fmt: skip
    bid, accepted = mail("bid-airline-priced.json"), mail("customer-accepts.json")
    conn.execute("INSERT INTO carriers (name, kind, email, serves) VALUES ('c', 'airline', ?, '')", (bid.sender,))
    # Our own mails the captured ones answer: the bid request, the quote, a request to the rate desk.
    for mail_id, purpose, to in (
        (bid.in_reply_to, "bid_request", bid.sender),
        (accepted.in_reply_to, "quote", rfq.sender),
    ):
        _store(ctx, rfq.model_copy(update={"id": mail_id, "direction": "out", "to": to}), purpose)
    desk = {"id": 902, "direction": "out", "to": RATEDESK, "subject": "Rate request FRA-JFK, 340 kg"}
    _store(ctx, rfq.model_copy(update=desk), "bid_request")
    return ctx


@pytest.mark.parametrize("name", EXPECTED)
async def test_rules_place_every_fixture(ctx, name):
    message = poller.ingest(ctx, mail(name)) if name != RFQ else mail(name)
    assert await classify(ctx, message) == EXPECTED[name]


@pytest.mark.parametrize("case", ["rfq_subject_mentions_booking_confirmed", "booking_confirmation_reworded"])
async def test_a_booking_confirmation_comes_from_an_airline_never_from_a_customer(ctx, case):
    probe = next(
        p
        for line in (MAILS / "routing_probes.jsonl").read_text().splitlines()
        if (p := json.loads(line))["case_id"] == case
    )
    message = to_message({"id": 990, "sent_at": "2026-11-21T10:00:00Z", **probe["mail"]}, "in")
    if probe["setup"]["sender_on_carrier_list"]:
        ctx.db.execute(
            "INSERT INTO carriers (name, kind, email, serves) VALUES ('a', 'airline', ?, '')", (message.sender,)
        )
    assert (await classify(ctx, poller.ingest(ctx, message))).value == probe["expected"]


@pytest.mark.parametrize(
    "body",
    ["Unfortunately the booking could not be confirmed: no space on this flight.",
     "Booking reference: -\nStatus: rejected, the flight is full."],
)  # fmt: skip
async def test_an_airline_refusing_our_booking_by_mail_confirms_nothing(ctx, body):
    # Routed to the booking's own handler, which reads the refusal: the booking fails for a person instead of
    # staying requested as a carrier bid, and is never confirmed.
    rfq, airline = mail(RFQ), mail("bid-airline-priced.json").sender
    _store(ctx, rfq.model_copy(update={"id": 4800, "direction": "out", "to": airline}), "booking")
    quotes.save(ctx, quote(rfq.session_id))
    bookings.save(ctx, Booking(id="B-PX-E5F36E", quote_id=f"Q-{rfq.session_id}", session_id=rfq.session_id,
                               team_reference="PX-E5F36E", via="email", carrier="Kestrel Airways",
                               created_at=datetime(2026, 11, 21, 17, 39, tzinfo=UTC)))  # fmt: skip
    raw = {"id": 4801, "from": airline, "to": "pixel@hackloop.mail", "subject": "Re: Booking request PX-E5F36E",
           "body": body, "in_reply_to": 4800, "references": [4800], "received_at": "2026-11-21T17:40:00+00:00",
           "attachments": []}  # fmt: skip
    message = poller.ingest(ctx, to_message(raw, "in"))
    assert await classify(ctx, message) == MailKind.BOOKING_CONFIRMATION
    await booking.handle_confirmation(ctx, message)
    assert bookings.get(ctx, "B-PX-E5F36E").status == BookingStatus.FAILED
    assert [r["kind"] for r in ctx.db.execute("SELECT kind FROM events ORDER BY id")][-2:] == [
        "booking.failed", "human.note"]  # fmt: skip


async def test_carrier_answer_joins_the_rfq_session(ctx):
    assert poller.ingest(ctx, mail("bid-airline-priced.json")).session_id == mail(RFQ).id


async def test_the_rate_desks_answer_joins_the_session_of_the_request_it_names(ctx):
    assert poller.ingest(ctx, mail("ratedesk-portal-rates.json")).session_id == mail(RFQ).id


async def test_unclear_new_thread_asks_the_model_once(ctx, monkeypatch):
    calls = []

    async def model(ctx, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", model)
    message = poller.ingest(ctx, mail("synthetic-rfq-unclear.json"))
    assert await classify(ctx, message) == MailKind.CUSTOMER_RFQ  # a failed call never loses a request
    assert len(calls) == 1


async def test_a_request_in_tonnes_between_cities_is_a_request_by_rules(ctx):
    raw = {"id": 990, "from": "buyer@hackloop.mail", "to": "pixel@hackloop.mail", "subject": "Air freight",
           "body": "Hello, we need to ship 1.2 t of machine parts from Munich to Tokyo.", "in_reply_to": None,
           "references": [], "received_at": "2026-11-21T16:00:00+00:00", "attachments": []}  # fmt: skip
    assert await classify(ctx, poller.ingest(ctx, to_message(raw, "in"))) == MailKind.CUSTOMER_RFQ


async def test_the_airlines_answer_to_our_booking_by_mail_is_a_confirmation(ctx):
    # Round 27: Meridian's "Status: booked" reply to our booking request was read as a bid.
    rfq, airline = mail(RFQ), mail("bid-airline-priced.json").sender
    _store(ctx, rfq.model_copy(update={"id": 4800, "direction": "out", "to": airline}), "booking")
    raw = {"id": 4801, "from": airline, "to": "pixel@hackloop.mail", "subject": "Re: Booking request PX-E5F36E",
           "body": "Booking reference: BK-43VLF40X\nStatus: booked", "in_reply_to": 4800, "references": [4800],
           "received_at": "2026-11-21T17:40:00+00:00", "attachments": []}  # fmt: skip
    assert await classify(ctx, poller.ingest(ctx, to_message(raw, "in"))) == MailKind.BOOKING_CONFIRMATION


async def test_the_customers_answer_to_our_question_is_left_to_the_waiting_request(ctx):
    rfq = mail(RFQ)  # Lindqvist; her answer quotes our question 4723 in the thread
    _store(ctx, rfq.model_copy(update={"id": 4723, "direction": "out", "to": rfq.sender}), "question")
    answer = poller.ingest(ctx, mail("customer-names-pickup.json"))
    assert await classify(ctx, answer) == MailKind.OTHER


SHEET = (
    "Testline Cargo | Air export tariff\nCurrency    EUR\nValid from  01.11.2026\nValid to    30.11.2026\n\n"
    "Lane        Transit     Minimum     -45       +45\nCGN-SYD     5  EUR 75.00   EUR 5.92  EUR 5.03\n"
    "DUS-JFK     5  EUR 100.00  EUR 3.96  EUR 3.21\n"
)


@pytest.mark.parametrize(
    ("subject", "body", "kind"),
    [
        ("Tariff update", SHEET, "tariff"),  # an airline's tariff written in the mail itself
        ("Re: Rate request FRA-JFK, 340 kg", "FRA-JFK EUR 2.50/kg\nFRA-EWR EUR 2.60/kg\nTransit 3 days", "carrier_bid"),
        ("Rates for you", "We can fly FRA-JFK for EUR 950.00 all-in, transit 3 days.", "carrier_bid"),  # one lane
    ],
)
async def test_an_airlines_tariff_in_the_mail_itself_is_a_tariff_and_a_bid_stays_a_bid(ctx, subject, body, kind):
    message = to_message({"id": 991, "from": "air@carrier.example", "to": "us@example.com", "subject": subject,
                          "body": body, "sent_at": "2026-11-21T10:00:00Z"}, "in")  # fmt: skip
    ctx.db.execute("INSERT INTO carriers (name, kind, email, serves) VALUES ('a', 'airline', ?, '')", (message.sender,))
    assert (await classify(ctx, poller.ingest(ctx, message))).value == kind
