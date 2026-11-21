import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LlmResult, Message, Preference
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import store
from pixel.pricing.pickup import zone_of

RECEIVED = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
DEADLINE = deadline_for(RECEIVED, settings())


def mail(body: str, subject: str = "Request for quotation") -> Message:
    return Message(
        id=7, session_id=7, direction="in", sender="buyer@example.com", to="us@example.com",
        subject=subject, body=body, sent_at=RECEIVED,
    )  # fmt: skip


@pytest.fixture
def emitted(monkeypatch):
    log = []
    monkeypatch.setattr(events, "emit", lambda ctx, kind, actor, **kw: log.append((kind, kw)))
    return log


def test_plain_english_request():
    rfq = extract.by_rules(mail("Please quote AAA-BBB, 3 pallets, 1,250 kg, 2.5 cbm. Thanks!"), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == ("AAA", "BBB", Decimal(1250), Decimal("2.5"))
    assert (rfq.pieces, rfq.preference, rfq.language, rfq.currency) == (3, Preference.CHEAPEST, "en", None)
    assert rfq.deadline_at == DEADLINE.expires_at


def test_german_door_pickup_fastest():
    body = (
        "Guten Tag, bitte um Ihr schnellstes Angebot von Musterstadt (AAA) nach Beispielhafen (BBB). "
        "Gewicht 1.250 kg, Volumen 4,2 m³. Abholung in 12345 Musterstadt. Danke und Grüße"
    )
    rfq = extract.by_rules(mail(body), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == ("AAA", "BBB", Decimal(1250), Decimal("4.2"))
    assert rfq.preference == Preference.FASTEST
    assert (rfq.door_pickup, rfq.pickup_postcode, rfq.pickup_zone, rfq.language) == (True, "12345", "12", "de")


def test_decimal_comma_weight_and_currency():
    rfq = extract.by_rules(mail("Origin: AAA\nDestination: BBB\nWeight: 12,5 kg\nPlease quote in USD."), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.currency) == ("AAA", "BBB", Decimal("12.5"), "USD")


def test_two_weights_are_not_certain():
    assert extract.by_rules(mail("AAA to BBB, 2 boxes: one 40 kg, one 60 kg"), DEADLINE) is None


async def test_unreadable_request_takes_one_model_call(monkeypatch, emitted):
    calls = []

    async def fake_ask(ctx, **kw):
        calls.append(kw)
        data = {"origin": "aaa", "destination": "BBB", "weight_kg": "300", "volume_cbm": None, "pieces": None,
                "preference": "fastest", "door_pickup": False, "pickup_postcode": None, "currency": None,
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    rfq = await extract.extract(
        None, mail("We need to move three hundred kilos from our plant to the client"), DEADLINE
    )
    assert len(calls) == 1 and calls[0]["purpose"] == "rfq.extract"
    assert (rfq.origin, rfq.weight_kg, rfq.extracted_by, rfq.missing) == ("AAA", Decimal(300), "model", ())
    assert emitted[0][0] == "rfq.extracted"


async def test_rules_path_makes_no_model_call(monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    rfq = await extract.extract(None, mail("AAA → BBB, 500 kg"), DEADLINE)
    assert rfq.extracted_by == "rules"


async def test_a_captured_door_request_without_an_origin_takes_the_pickup_zones_airport(conn, monkeypatch, emitted):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / "rfq-door-no-origin.json").read_text())
    message = to_message(raw, "in")
    assert extract.by_rules(message, DEADLINE) is None  # no origin airport in the mail

    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": None, "destination": "ORD", "weight_kg": "676", "volume_cbm": "5.79", "pieces": 12,
                "preference": "fastest", "door_pickup": True, "pickup_postcode": "51447", "currency": "EUR",
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("51447"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, message, DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.door_pickup, rfq.missing) == ("AAA", "ORD", True, ())


def test_rules_read_the_captured_port_to_port_request():
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / "rfq-port-to-port.json").read_text())
    rfq = extract.by_rules(to_message(raw, "in"), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == (
        "DUS",
        "JFK",
        Decimal(1540),
        Decimal("5.90"),
    )
    assert (rfq.preference, rfq.door_pickup, rfq.currency) == (Preference.FASTEST, False, "EUR")
