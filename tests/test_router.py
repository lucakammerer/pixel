import json
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.router.agent import classify
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import MailKind
from pixel.core import poller
from pixel.core.context import Context
from pixel.core.outbox import _store

MAILS = Path(__file__).parent / "fixtures" / "mails"
EXPECTED = {
    "synthetic-rfq.json": MailKind.CUSTOMER_RFQ,
    "bid-airline-priced.json": MailKind.CARRIER_BID,
    "bid-decline-lane.json": MailKind.CARRIER_BID,
    "synthetic-customer-reply.json": MailKind.CUSTOMER_REPLY,
    "synthetic-booking-confirmation.json": MailKind.BOOKING_CONFIRMATION,
    "synthetic-tariff.json": MailKind.TARIFF,
    "synthetic-tracking.json": MailKind.TRACKING_QUESTION,
    "synthetic-ratedesk.json": MailKind.RATEDESK_REPLY,
}


def mail(name: str):
    return to_message(json.loads((MAILS / name).read_text()), "in")


@pytest.fixture
def ctx(conn, monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("the rules must place this mail")

    monkeypatch.setattr(llm, "ask_json", no_model)
    ctx = Context(settings(), conn, None, None, None, None)
    rfq = poller.ingest(ctx, mail("synthetic-rfq.json"))
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (?, ?, 'quoted', ?)",
                 (rfq.id, rfq.sender, rfq.sent_at.isoformat()))  # fmt: skip
    bid = mail("bid-airline-priced.json")
    conn.execute("INSERT INTO carriers (name, kind, email, serves) VALUES ('c', 'airline', ?, '')", (bid.sender,))
    ours = ((bid.in_reply_to, "bid_request", bid.sender), (901, "quote", rfq.sender), (902, "bid_request", ""))
    for mail_id, purpose, to in ours:
        _store(ctx, rfq.model_copy(update={"id": mail_id, "direction": "out", "to": to}), purpose)
    return ctx


@pytest.mark.parametrize("name", EXPECTED)
async def test_rules_place_every_fixture(ctx, name):
    message = poller.ingest(ctx, mail(name)) if name != "synthetic-rfq.json" else mail(name)
    assert await classify(ctx, message) == EXPECTED[name]


async def test_carrier_answer_joins_the_rfq_session(ctx):
    assert poller.ingest(ctx, mail("bid-airline-priced.json")).session_id == 1001


async def test_unclear_new_thread_asks_the_model_once(ctx, monkeypatch):
    calls = []

    async def model(ctx, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", model)
    message = poller.ingest(ctx, mail("synthetic-rfq-unclear.json"))
    assert await classify(ctx, message) == MailKind.CUSTOMER_RFQ  # a failed call never loses a request
    assert len(calls) == 1
