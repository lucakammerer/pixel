import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import count
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import agent
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LaneTariff, LlmResult, Message, Rfq, Source
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import bids, engine, portal_rates, store

NOW = datetime.now(UTC)
IDS = count(100)


def deadline(seconds: float = 3) -> Deadline:
    target = datetime.now(UTC) + timedelta(seconds=seconds)
    return Deadline(received_at=NOW, target_at=target, hard_at=target, expires_at=target)


def mail(sender: str, body: str, session_id: int = 1) -> Message:
    return Message(id=next(IDS), session_id=session_id, direction="in", sender=sender, to="us@x", subject="Re",
                   body=body, sent_at=NOW)  # fmt: skip


def rfq(**kw) -> Rfq:
    return Rfq(session_id=1, message_id=1, customer="c@x", origin="AAA", destination="BBB", weight_kg=D(100),
               received_at=NOW, deadline_at=NOW, **kw)  # fmt: skip


class FakePortal:
    rate_remaining = None

    async def lanes(self):
        return []


@pytest.fixture
def ctx(conn, monkeypatch):
    def emit(ctx, kind, actor, *, session_id=None, message_id=None, payload=None, **metrics):
        ctx.db.execute(
            "INSERT INTO events (ts, session_id, kind, actor, message_id, payload, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), session_id, kind, actor, message_id, json.dumps(payload or {}),
             str(metrics.get("cost_usd", 0))),
        )  # fmt: skip

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        return mail("us@x", body, session_id).model_copy(update={"to": to})

    monkeypatch.setattr(events, "emit", emit)
    monkeypatch.setattr(outbox, "send", send)
    monkeypatch.setattr(portal_rates, "_lanes", None)
    store.save_carriers(ctx := Context(settings(), conn, None, FakePortal(), None, None), [
        {"name": "Air One", "kind": "airline", "email": "one@air", "serves": "BBB CCC"},
        {"name": "Air Two", "kind": "airline", "email": "two@air", "serves": "BBB"},
        {"name": "Truck A", "kind": "trucker", "email": "a@truck", "serves": "12 13"},
        {"name": "Truck B", "kind": "trucker", "email": "b@truck", "serves": "12"},
    ])  # fmt: skip
    store.save_zones(ctx, [{"zone": "12", "area": "Town", "airport": "AAA"}])
    return ctx


def kinds(ctx, kind):
    return [json.loads(r[0]) for r in ctx.db.execute("SELECT payload FROM events WHERE kind = ?", (kind,))]


@pytest.mark.parametrize(
    ("body", "total", "currency", "days"),
    [
        ("Hello,\nour all-in price is EUR 1.234,50, transit 3 days.\nRegards", "1234.50", "EUR", 3),
        ("Freight 2.10/kg\nTotal: 980.00 USD (1 USD = 0.92 EUR)\nTransit time: 4 days", "980.00", "USD", 4),
        ("We offer 845,00 € all in, 2 Tage Laufzeit.\n\n> Please quote 100 kg, EUR 5 per kg", "845.00", "EUR", 2),
    ],
)
def test_rules_read_a_bid(body, total, currency, days):
    option = bids.parse(mail("one@air", body), rfq())
    assert (option.total, option.currency, option.transit_days) == (D(total), currency, days)
    assert option.total_eur == (D(total) if currency == "EUR" else D("901.60"))


@pytest.mark.parametrize(
    "body",
    ["Freight EUR 500.00\nFuel EUR 80.00\n3 days", "Sorry, we cannot offer this lane.", "USD 900, 3 days"],
)
def test_rules_refuse_what_they_cannot_read(body):
    assert bids.parse(mail("one@air", body), rfq()) is None


async def test_unpriced_destination_is_mailed_and_door_to_port_adds_the_cheapest_pickup(ctx, monkeypatch):
    request = rfq(door_pickup=True, pickup_postcode="12345", pickup_zone="12")
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))

    async def model(ctx, **kw):
        return LlmResult(data={"offer": True, "total": None, "components": ["EUR 500.00", "EUR 80.00"],
                               "currency": "EUR", "transit_days": "5", "eur_per_unit": None},
                         model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", model)

    async def answer():
        while len(kinds(ctx, "bid.requested")) < 4:
            await asyncio.sleep(0.01)
        await bids.record(ctx, mail("one@air", "All-in EUR 700.00, transit 2 days"))
        await bids.record(ctx, mail("two@air", "Freight EUR 500.00\nFuel EUR 80.00\n5 days"))
        await bids.record(ctx, mail("a@truck", "Pickup EUR 90.00"))
        await bids.record(ctx, mail("b@truck", "We can do it for 75,00 EUR"))

    found, _ = await asyncio.gather(engine.options(ctx, request, deadline()), answer())
    assert {r["carrier"] for r in kinds(ctx, "bid.requested")} == {"Air One", "Air Two", "Truck A", "Truck B"}
    by_carrier = {o.carrier: o for o in found}
    assert set(by_carrier) == {"Air One", "Air Two"}
    assert (by_carrier["Air One"].total, by_carrier["Air One"].trucker) == (D("775.00"), "Truck B")
    assert by_carrier["Air Two"].total == D("655.00")
    assert [r["read_by"] for r in kinds(ctx, "bid.received")].count("model") == 1


async def test_quote_from_a_sheet_with_one_gateway_call_and_the_fallback_without_one(ctx, monkeypatch):
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    ctx.db.execute("DELETE FROM carriers")
    calls = []

    async def model(ctx, **kw):
        calls.append(kw["purpose"])
        events.emit(ctx, "llm.call", "llm", session_id=kw["session_id"], payload={}, cost_usd=D("0.0001"))
        return LlmResult(data={"sentence": "Thanks for thinking of us."}, model="m", tokens_in=1, tokens_out=1,
                         cost_usd=D("0.0001"))  # fmt: skip

    async def send_quote(ctx, quote):
        return quote.model_copy(update={"sent_at": datetime.now(UTC), "sent_message_id": 9})

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    request = mail("c@x", "Please quote AAA-BBB, 100 kg.", session_id=1)
    started = datetime.now(UTC)

    quote = await agent.handle(ctx, request, deadline(30))
    assert (datetime.now(UTC) - started).total_seconds() < 10
    assert (quote.total, quote.currency, quote.option.source, quote.llm_calls) == (D("260.00"), "EUR", Source.SHEET, 1)
    assert "260.00 EUR" in quote.body and calls == ["quote.opening"]

    assert quotes.for_session(ctx, 1) == quote

    backup = await agent.fallback(ctx, 1)
    assert (backup.total, backup.fallback, len(calls)) == (D("260.00"), True, 1)


def captured(name: str) -> Message:
    return to_message(json.loads((Path(__file__).parent / "fixtures" / "mails" / name).read_text()), "in")


def test_a_captured_lane_decline_is_recognised_without_a_model():
    assert bids.DECLINE.search(captured("bid-decline-lane.json").body)


def test_rules_read_the_captured_airline_bid_and_pickup_bid():
    """The airline writes its total ungrouped; both mails carry a validity time with a time zone after a number."""
    airline = bids.parse(captured("bid-airline-priced.json"), rfq())
    assert (airline.total, airline.currency, airline.transit_days) == (D("1195.90"), "EUR", 3)
    pickup = bids.parse(captured("bid-pickup-priced.json"), rfq(), needs_transit=False)
    assert (pickup.total, pickup.currency) == (D("182.62"), "EUR")
