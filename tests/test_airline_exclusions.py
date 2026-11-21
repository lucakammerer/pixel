from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from test_outbox import make_ctx, make_quote

from pixel.agents.rfq import agent
from pixel.api.mailbox import to_message
from pixel.contracts import Source
from pixel.core import events, poller
from pixel.core.orchestrator import Deadline
from pixel.pricing import engine, exclusions, selection

BLOCKED = "Prohibited Air Cargo"
ALLOWED = "Alternative Air Cargo"
CUSTOMER = "customer@example.test"
NOW = datetime.now(UTC)


def request(ctx, mid, text, customer=CUSTOMER):
    message = poller.ingest(ctx, to_message({"id": mid, "from": customer, "to": "pixel@example.test",
                                          "subject": "Quote request", "body": text,
                                          "created_at": NOW.isoformat()}, "in"))  # fmt: skip
    return make_quote(message).rfq


def options(rfq):
    base = make_quote(to_message({"id": rfq.message_id, "from": rfq.customer, "to": "pixel@example.test",
                                  "subject": "Quote request", "body": "cargo", "created_at": NOW.isoformat()},
                                 "in")).option  # fmt: skip
    cheap = base.model_copy(update={"carrier": BLOCKED, "source": Source.BID})
    other = base.model_copy(update={"carrier": ALLOWED, "source": Source.BID, "total_eur": Decimal(140)})
    return cheap, other


async def test_current_and_future_quotes_exclude_for_that_customer_only(conn):
    ctx = make_ctx(conn)
    first = request(
        ctx, 101, f"Please do not offer {BLOCKED} (XY) for this shipment or for any of our future inquiries."
    )
    next_ = request(ctx, 102, "Please send your cheapest quote.")
    unrelated = request(ctx, 103, "Please send your cheapest quote.", "different@example.test")
    for rfq, want in [(first, ALLOWED), (next_, ALLOWED), (unrelated, BLOCKED)]:
        cheap, other = options(rfq)
        events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
        for option in [cheap, other]:
            events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                        payload={"option": option.model_dump(mode="json")})  # fmt: skip
        fallback = await agent.fallback(ctx, rfq.session_id)
        assert fallback.option.carrier == want
    assert exclusions.for_request(ctx, next_).names("Different display name", "XY")


def test_current_only_quoted_and_positive_mentions_are_not_standing_exclusions(conn):
    ctx = make_ctx(conn)
    first = request(ctx, 201, f"Please do not use {BLOCKED} for this shipment. {ALLOWED} is fine.")
    next_ = request(
        ctx, 202, f"We prefer {BLOCKED}.\n\nOn Monday someone wrote:\n> Please avoid {BLOCKED} going forward."
    )
    assert exclusions.for_request(ctx, first).names(BLOCKED)
    assert not exclusions.for_request(ctx, first).names(ALLOWED)
    assert not exclusions.for_request(ctx, next_).names(BLOCKED)


async def test_engine_does_not_request_banned_airline_or_quote_cached_options(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = request(ctx, 301, f"Please do not offer {BLOCKED} (XY).")
    cheap, other = options(rfq)
    desk = cheap.model_copy(update={"source": Source.RATEDESK, "carrier_code": "XY", "carrier": "Alias Cargo"})
    portal = desk.model_copy(update={"source": Source.PORTAL})
    sheet = cheap.model_copy(update={"source": Source.SHEET})
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": cheap.model_dump(mode="json")})  # fmt: skip
    monkeypatch.setattr(engine, "sheet_options", lambda *_: [sheet])
    monkeypatch.setattr(engine.portal_rates, "options", AsyncMock(return_value=[portal]))
    monkeypatch.setattr(engine.pickup, "options", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine.store, "carriers", lambda *_: [(BLOCKED, "blocked@x"), (ALLOWED, "allowed@x")])
    mailed = AsyncMock(return_value=[1])
    monkeypatch.setattr(engine.bids, "request", mailed)
    monkeypatch.setattr(engine.bids, "collect", AsyncMock(return_value=[cheap, desk, other]))
    until = NOW + timedelta(minutes=1)
    available = await engine.options(ctx, rfq, Deadline(NOW, until, until, until))
    assert available == [other]
    assert selection.select(rfq, available).carrier == ALLOWED
    mailed.assert_awaited_once_with(ctx, rfq, [(ALLOWED, "allowed@x")])
