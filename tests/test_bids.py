import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import count
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import agent, extract
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LaneTariff, LlmResult, Message, RateOption, Rfq, Source
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import bids, engine, portal_rates, store

NOW = datetime.now(UTC)
IDS = count(100)


@pytest.mark.parametrize(
    "phrase,hours",
    [
        ("Pickup time: 11 hours", 11),
        ("pick up within 25 hours", 25),
        ("collect within 12 hours", 12),
        ("Abholung in 12 Stunden", 12),
        ("Ready in 9 hrs", 9),
        ("We can collect.", None),
    ],
)
def test_pickup_hours_ignore_offer_validity(phrase, hours):
    assert bids._pickup_hours(f"Offer valid for 24 hours. {phrase}") == hours


def test_combine_balances_timed_pickup_bids():
    request = rfq(door_pickup=True)
    airline = RateOption(
        source=Source.PORTAL, carrier="Air", origin=request.origin, destination=request.destination,
        chargeable_kg=D(100), billed_kg=D(100), freight=D(100), total=D(100), currency="EUR", total_eur=D(100),
        transit_days=1,
    )  # fmt: skip
    pickups = [
        airline.model_copy(update={"carrier": name, "trucker": name, "pickup_hours": hours, "total_eur": D(price)})
        for name, price, hours in [("Slow", 20, 24), ("Fast", 25, 8), ("Other", 30, 16), ("Untimed", 10, None)]
    ]
    [combined] = engine.combine(request, [airline, *pickups])
    assert (combined.trucker, combined.pickup_hours, combined.total_eur) == ("Fast", 8, D(125))
    [fallback] = engine.combine(request, [airline, pickups[1], pickups[-1]])
    assert fallback.trucker == "Untimed"


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


async def test_a_pickup_known_by_its_town_alone_is_asked_with_a_postcode_of_its_zone(ctx, monkeypatch):
    # Round 15: every trucker answered "postcode - (zone 93)" with "To quote we need: the pickup postcode".
    asked = []

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        asked.append(f"{subject}\n{body}")
        return mail("us@x", body, session_id)

    monkeypatch.setattr(extract, "ASK_WAIT_S", 0)  # the customer's answer is not part of this test
    store.save_zones(ctx, [{"zone": "93", "area": "Regensburg", "airport": "AAA"}])
    path = Path(__file__).parent / "fixtures" / "mails" / "rfq-door-warehouse-no-postcode.json"
    request = await extract.extract(ctx, to_message(json.loads(path.read_text()), "in"), deadline())
    monkeypatch.setattr(outbox, "send", send)
    await bids.request(ctx, request, [("Truck A", "a@truck")], pickup=True)
    assert (request.pickup_zone, request.pickup_postcode) == ("93", None)
    assert asked and all(len(re.findall(r"\b93\d{3}\b", text)) == 2 for text in asked)


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
        return LlmResult(data={"first_name": None, "intro": "Thanks for thinking of us.", "closing": None}, model="m", tokens_in=1, tokens_out=1,
                         cost_usd=D("0.0001"))  # fmt: skip

    async def send_quote(ctx, quote):
        return quote.model_copy(update={"sent_at": datetime.now(UTC), "sent_message_id": 9})

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    request = mail("c@x", "Please quote AAA-BBB, 100 kg, 0.3 cbm.", session_id=1)
    started = datetime.now(UTC)

    quote = await agent.handle(ctx, request, deadline(30))
    assert (datetime.now(UTC) - started).total_seconds() < 10
    assert (quote.total, quote.currency, quote.option.source, quote.llm_calls) == (D("260.00"), "EUR", Source.SHEET, 1)
    assert "260.00 EUR" in quote.body and calls == ["quote.prose"]

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
    labelled = bids.parse(captured("bid-airline-priced-labelled.json"), rfq())
    assert (labelled.total, labelled.currency, labelled.transit_days) == (D("586.80"), "EUR", 6)
    pickup = bids.parse(captured("bid-pickup-priced.json"), rfq(), needs_transit=False)
    assert (pickup.total, pickup.currency) == (D("182.62"), "EUR")


async def test_the_captured_rate_desk_answer_gives_one_option_per_priced_airline_without_a_model(ctx, monkeypatch):
    async def no_model(ctx, **kw):
        raise AssertionError("the rules must read the desk's answer")

    monkeypatch.setattr(llm, "ask_json", no_model)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq().model_dump(mode="json"))
    await bids.record(ctx, captured("ratedesk-portal-rates.json").model_copy(update={"session_id": 1}))
    found = [o["option"] for o in kinds(ctx, "option.found")]
    assert [(o["carrier_code"], o["total"], o["transit_days"], o["source"]) for o in found] == [
        ("PL", "952.00", 4, "ratedesk"),
        ("SL", "1149.20", 2, "ratedesk"),
    ]
    assert found[0]["rate_id"] == "R-C5nfsmcn3aSHlsgp" and found[0]["valid_until"].startswith("2026-11-21T14:38")
    assert [r["priced"] for r in kinds(ctx, "bid.received")] == [True]


async def test_model_read_request_drafts_its_prose_in_the_same_call(ctx, monkeypatch):
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    ctx.db.execute("DELETE FROM carriers")
    calls = []
    intro = "Thanks for the note about the spare parts, we will keep this simple for you."

    async def model(ctx, **kw):
        calls.append(kw["purpose"])
        events.emit(ctx, "llm.call", "llm", session_id=kw["session_id"], payload={}, cost_usd=D("0.0001"))
        data = {"origin": "AAA", "destination": "BBB", "weight_kg": "100", "volume_cbm": "0.3", "pieces": None,
                "preference": "cheapest", "door_pickup": False, "pickup_postcode": None, "currency": None,
                "language": "en", "first_name": "Tom", "intro": intro, "closing": "Call me for 2 minutes."}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=D("0.0001"))

    async def send_quote(ctx, quote):
        return quote.model_copy(update={"sent_at": datetime.now(UTC), "sent_message_id": 9})

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    request = mail("c@x", "Some spare parts from our usual airport to the other one, a hundred kilos, 0.3 cbm.\n\nTom")

    quote = await agent.handle(ctx, request, deadline(30))
    assert calls == ["rfq.extract"] and quote.llm_calls == 1
    # the extraction's opening names no phrase of the mail it answers (`hook`), so the quote opens with the price
    assert quote.body.startswith("Hello Tom,\n\n") and intro not in quote.body
    assert "2 minutes" not in quote.body
    assert "260.00 EUR" in quote.body

    backup = await agent.fallback(ctx, 1)
    assert backup.body.startswith("Hello Tom,") and len(calls) == 1


async def test_a_hanging_prose_call_never_holds_the_quote(ctx, monkeypatch):
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    ctx.db.execute("DELETE FROM carriers")

    async def hangs(ctx, **kw):
        await asyncio.sleep(60)

    async def send_quote(ctx, quote):
        return quote.model_copy(update={"sent_at": datetime.now(UTC), "sent_message_id": 9})

    monkeypatch.setattr(agent, "PROSE_WAIT_S", 0.3)
    monkeypatch.setattr(llm, "ask_json", hangs)
    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    started = datetime.now(UTC)
    quote = await agent.handle(ctx, mail("c@x", "Please quote AAA-BBB, 100 kg, 0.3 cbm."), deadline(30))
    assert (datetime.now(UTC) - started).total_seconds() < 2
    assert quote.body.startswith("Hello,\n\n") and "260.00 EUR" in quote.body


async def test_a_late_start_still_waits_for_the_pickup_and_mailed_bids(ctx, monkeypatch):
    # Round 17: handling started 75 s after receipt, the truckers answered 30 s after the ask, and the quote had
    # already gone out at target_at with no pickup. Scaled: target 0.1 s away, answers 0.5 s after the ask.
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    ctx.settings = replace(ctx.settings, bid_wait_s=2)
    request = rfq(door_pickup=True, pickup_postcode="12345", pickup_zone="12")
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    now = datetime.now(UTC)
    late = Deadline(received_at=now - timedelta(seconds=75), target_at=now + timedelta(seconds=0.1),
                    hard_at=now + timedelta(seconds=5), expires_at=now + timedelta(seconds=6))  # fmt: skip

    async def answer():
        while len(kinds(ctx, "bid.requested")) < 3:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await bids.record(ctx, mail("two@air", "All-in EUR 400.00, transit 4 days"))
        await bids.record(ctx, mail("a@truck", "Pickup EUR 90.00"))
        await bids.record(ctx, mail("b@truck", "We can do it for 75,00 EUR"))

    found, _ = await asyncio.gather(engine.options(ctx, request, late), answer())
    by_carrier = {o.carrier: o for o in found}
    assert set(by_carrier) == {"Air One", "Air Two"}
    assert all(o.pickup == D("75.00") for o in found)


@pytest.mark.parametrize(
    "text, price, days",
    [
        ("All-in EUR 850.00. Transit: 3 working days.", D("850.00"), 3),
        ("EUR 850.00 per shipment, all-in. Transit 2-3 business days.", D("850.00"), 2),
        ("All-in price EUR 850.00 per AWB, transit 3d.", D("850.00"), 3),
    ],
)
def test_a_flat_bid_in_other_words_keeps_its_price_and_transit(text, price, days):
    assert bids._price(text, rfq()) == (price, "EUR") and bids._days(text) == days


@pytest.mark.parametrize(
    "text, hours",
    [("Pickup time: 10.5 hours", 11), ("Abholung in 4,5 Stunden", 5), ("Ready at 09:00 h, pickup time 18 hours", 18)],
)
def test_pickup_hours_read_a_decimal_and_skip_a_clock_time(text, hours):
    assert bids._pickup_hours(text) == hours  # 10.5 read as 5 and 09:00 h as 0 pick the wrong trucker


def test_bid_transit_ignores_validity_period():
    body = "EUR 900.00 all-in, offer valid for 7 days, transit 3 days."
    assert bids._days(body) == 3
    body2 = "EUR 900.00 all-in, Valid 7 days, 3 days transit."
    assert bids._days(body2) == 3
