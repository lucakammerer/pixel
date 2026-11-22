import asyncio
import base64
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import count
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import agent, extract, unmet
from pixel.agents.router import agent as router
from pixel.agents.tariffs import agent as tariffs
from pixel.agents.tariffs.agent import _spoken
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import Attachment, LaneTariff, LlmResult, Message, RateOption, Rfq, Source
from pixel.core import events, outbox, timing
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


async def test_explicit_lane_suspension_applies_only_from_effective_date_until_resumption(ctx):
    request = rfq()
    day = NOW.date()
    effective = day + timedelta(days=1)
    tariff = LaneTariff(carrier="Air One", origin=request.origin, destination=request.destination, currency="EUR",
                        valid_from=day, valid_to=day + timedelta(days=10), version="original", transit_days=2,
                        minimum=D(0), breaks=((D(0), D(2)),), fuel_per_kg=D("0.10"), security_per_kg=D(0),
                        source="test")  # fmt: skip
    store.upsert(ctx, [tariff])
    [old] = engine.sheet_options(ctx, request)
    lane = f"{request.origin}-{request.destination}"
    notice = mail("one@air", f"New bookings on {lane} suspended from {effective}.")
    assert router.rules(ctx, notice)[0].value == "tariff"
    await tariffs.handle(ctx, notice.model_copy(update={"sender": "unknown@example.test"}))
    assert not kinds(ctx, "carrier.availability")
    await tariffs.handle(ctx, notice)
    assert engine.combine(request, [old], ctx=ctx) == [old]

    token = timing.clock.set(lambda: NOW + timedelta(days=1))
    try:
        assert not engine.sheet_options(ctx, request) and not engine.combine(request, [old], ctx=ctx)
        await tariffs.handle(ctx, mail("one@air", f"New bookings on {lane} resumed on {effective}."))
        assert engine.combine(request, [old], ctx=ctx) == [old]
    finally:
        timing.clock.reset(token)


@pytest.mark.parametrize(
    ("body", "total", "currency", "days"),
    [
        ("Hello,\nour all-in price is EUR 1.234,50, transit 3 days.\nRegards", "1234.50", "EUR", 3),
        ("Freight 2.10/kg\nTotal: 980.00 USD (1 USD = 0.92 EUR)\nTransit time: 4 days", "980.00", "USD", 4),
        ("We offer 845,00 € all in, 2 Tage Laufzeit.\n\n> Please quote 100 kg, EUR 5 per kg", "845.00", "EUR", 2),
        ("EUR 900,00 all-in. Transitzeit: 3 Tage. Laufzeit des Angebots: 14 Tage.", "900.00", "EUR", 3),
        ("EUR 900.00 all-in, 2 days. Transit via DXB, 1 day stopover.", "900.00", "EUR", 2),
        ("We can offer 950.00 EUR per AWB all-in, transit 3 days.", "950.00", "EUR", 3),  # per AWB: the flat total
        ("We can offer 950 EUR per AWB all-in, transit 3 days.", "950.00", "EUR", 3),
        ("Total 1,250.00 EUR/AWB, transit 3 days.", "1250.00", "EUR", 3),
        ("Rate 2.10 EUR/KGS all-in, transit 3 days.", "210.00", "EUR", 3),  # per KGS: a rate per kg
        ("Freight EUR 500.00\nFuel EUR 80.00\n3 days", "580.00", "EUR", 3),  # charge lines without a total add up
    ],
)
def test_rules_read_a_bid(body, total, currency, days):
    option = bids.parse(mail("one@air", body), rfq())
    assert (option.total, option.currency, option.transit_days) == (D(total), currency, days)
    assert option.total_eur == (D(total) if currency == "EUR" else D("901.60"))


@pytest.mark.parametrize(
    "body",
    [
        "Freight EUR 500.00\nFuel EUR 80.00\nFreight EUR 500.00\n3 days",
        "Sorry, we cannot offer this lane.",
        "USD 900, 3 days",
    ],
)
def test_rules_refuse_what_they_cannot_read(body):
    assert bids.parse(mail("one@air", body), rfq()) is None


async def test_unpriced_destination_is_mailed_and_door_to_port_adds_the_cheapest_pickup(ctx, monkeypatch):
    ctx.settings = replace(ctx.settings, model_check="")  # the rules alone: the model reads only Air Two
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
    assert [r["read_by"] for r in kinds(ctx, "bid.received")].count("model") == 0  # charge lines add up by rules


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
    # no attachment: no gpt-4.1 second reader; the last reader of request and reply (`unmet.py`) on the finished text
    assert "260.00 EUR" in quote.body and calls == ["quote.prose", "quote.unmet"]

    assert quotes.for_session(ctx, 1) == quote

    backup = await agent.fallback(ctx, 1)
    assert (backup.total, backup.fallback, len(calls)) == (D("260.00"), True, 2)


def captured(name: str) -> Message:
    return to_message(json.loads((Path(__file__).parent / "fixtures" / "mails" / name).read_text()), "in")


def test_a_captured_lane_decline_is_recognised_without_a_model():
    assert bids.DECLINE.search(captured("bid-decline-lane.json").body)


DECLINES = json.loads((Path(__file__).parent / "fixtures" / "lane-declines.json").read_text())["declines"]


@pytest.mark.parametrize("decline", DECLINES, ids=[d["shape"] for d in DECLINES])
def test_rules_read_the_lanes_a_declining_airline_flies_and_nothing_in_doubt(decline):
    body = captured(decline["mail"].removeprefix("mails/")).body if "mail" in decline else decline["body"]
    assert bids.offered_lanes(body) == decline["lanes"]


async def test_an_airline_is_skipped_only_while_its_fresh_list_leaves_the_lane_out(ctx, monkeypatch):
    """Air Two's list holds the lane: it must still be asked. Air One's leaves it out: skipped, for an hour."""
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    lane = f"{request.origin}-{request.destination}"
    await bids.record(ctx, mail("one@air", "We do not serve this lane/zone: XXX-YYY.\nWe fly XXX-ZZZ, ZZZ-YYY."))
    await bids.record(ctx, mail("two@air", f"We do not serve this lane/zone: XXX-YYY.\nWe fly XXX-ZZZ, {lane}."))
    assert [r["carrier"] for r in kinds(ctx, "carrier.lanes")] == ["Air One", "Air Two"]

    async def answer():
        while not kinds(ctx, "bid.requested"):
            await asyncio.sleep(0.01)
        await bids.record(ctx, mail("two@air", "All-in EUR 400.00, transit 4 days"))

    ctx.settings = replace(ctx.settings, model_check="")
    found, _ = await asyncio.gather(engine.options(ctx, request, deadline()), answer())
    assert [r["carrier"] for r in kinds(ctx, "bid.requested")] == ["Air Two"]
    assert [r["carrier"] for r in kinds(ctx, "bid.skipped")] == ["Air One"]
    assert [o.carrier for o in found] == ["Air Two"]

    stale = (NOW - bids.LANES_FRESH - timedelta(minutes=1)).isoformat()
    ctx.db.execute("UPDATE events SET ts = ? WHERE kind = 'carrier.lanes'", (stale,))
    carriers = store.carriers(ctx, "airline", request.destination)
    assert bids.worth_asking(ctx, request, carriers) == carriers


def test_rules_read_the_captured_airline_bid_and_pickup_bid():
    """The airline writes its total ungrouped; both mails carry a validity time with a time zone after a number."""
    airline = bids.parse(captured("bid-airline-priced.json"), rfq())
    assert (airline.total, airline.currency, airline.transit_days) == (D("1195.90"), "EUR", 3)
    labelled = bids.parse(captured("bid-airline-priced-labelled.json"), rfq())
    assert (labelled.total, labelled.currency, labelled.transit_days) == (D("586.80"), "EUR", 6)
    pickup = bids.parse(captured("bid-pickup-priced.json"), rfq(), needs_transit=False)
    assert (pickup.total, pickup.currency) == (D("182.62"), "EUR")


OFFERS = json.loads((Path(__file__).parent / "fixtures" / "pickup-offer-layouts.json").read_text())["offers"]


@pytest.mark.parametrize("offer", OFFERS, ids=[o["layout"] for o in OFFERS])
def test_rules_read_every_attached_offer_layout_and_never_the_waiting_time(offer):
    """Rounds 63 to 65: a letter, a table with its currency only in the header, a quotation, a voice message. "45.00
    EUR for each hour" of waiting read as the price would quote the pickup far too low; a model call per trucker
    costs spend."""
    text = offer.get("attachment_text") or "\n".join(_spoken(offer["transcript"]))
    body = bids._own_text(offer["body"]) + "\n" + text
    message = Message(id=9, session_id=1, direction="in", sender="t@x", to="us@x", subject="Re: Pickup", body=body,
                      sent_at=NOW)  # fmt: skip
    found = bids.parse(message, rfq(), needs_transit=False)
    assert (str(found.total), found.currency, bids._pickup_hours(body)) == (
        offer["total"], offer["currency"], offer["pickup_hours"])  # fmt: skip


async def test_a_pickup_price_in_an_attached_offer_is_read(ctx, monkeypatch):
    """Round 63: every trucker priced the pickup in an attachment (pdf, docx, mp3); the body says only "see the
    attachment". Read without the file, no pickup was priced and the airport-only fallback was rejected as too low."""
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / "bid-pickup-in-attachment.json").read_text())

    class Files:
        async def attachment(self, message_id, attachment_id):
            return base64.b64decode(raw["attachments"][0]["content_base64"])

    async def model(ctx, *, user, **kw):
        offer = "Total 165.70" in user
        data = {"offer": offer, "total": "165.70" if offer else None, "components": [], "currency": "EUR",
                "transit_days": None, "eur_per_unit": None}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(llm, "ask_json", model)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq(door_pickup=True).model_dump(mode="json"))
    message = to_message(raw, "in").model_copy(update={"session_id": 1, "sender": "a@truck"})
    await bids.record(replace(ctx, mailbox=Files()), message)
    [found] = [o["option"] for o in kinds(ctx, "option.found")]
    assert (found["pickup"], found["trucker"], found["pickup_hours"]) == ("165.70", "Truck A", 29)


PER_KG_TABLE = "\n".join(
    "  ".join(row)
    for row in [
        ["Portal rates AAA-BBB, 340.0 kg, 1.2 cbm"],
        ["Airline", "Code", "Rate per kg (EUR)", "Total (EUR)", "Transit (days)", "Rate id", "Valid until (UTC)"],
        ["Polaris Air Logistics", "PL", "2.80", "952.00", "4", "R-C5nfsmcn3aSHlsgp", "21 Nov 2026 14:38"],
    ]
)


def test_a_rate_per_kg_column_is_never_the_desk_total():
    found = bids.parse_desk(mail("ratedesk@hackloop.mail", f"Please find the rates attached.\n\n{PER_KG_TABLE}"), rfq())
    assert {o.total for o in found} <= {D("952.00")}


async def test_a_trucker_who_answers_only_in_a_photo_is_read_by_the_model_from_the_image(ctx, monkeypatch):
    """A photographed offer: no code reads an image outside a tariff mail, so the model reads its page, the one call a
    bid the rules cannot read costs anyway. Before, the trucker counted as answered and stayed unpriced."""
    import io

    from PIL import Image

    with io.BytesIO() as stream:
        Image.new("RGB", (40, 20), "white").save(stream, format="PNG")
        png = stream.getvalue()

    class Files:
        async def attachment(self, message_id, attachment_id):
            return png

    seen = []

    async def model(ctx, *, images=(), **kw):
        seen.append(images)
        data = {"offer": True, "total": "181.96 EUR", "components": [], "currency": "EUR", "transit_days": None,
                "eur_per_unit": None}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(llm, "ask_json", model)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq(door_pickup=True).model_dump(mode="json"))
    photo = Attachment(id=1, filename="offer-TRK-6203.jpg", content_type="image/jpeg", size=10)
    message = mail("a@truck", "Our offer TRK-6203 is in the attached photo.").model_copy(
        update={"attachments": (photo,)}
    )
    await bids.record(replace(ctx, mailbox=Files()), message)
    assert len(seen) == 1 and seen[0][0][:2] == b"\xff\xd8"  # the page as a JPEG
    [found] = [o["option"] for o in kinds(ctx, "option.found")]
    assert found["pickup"] == "181.96"


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
    assert calls == ["rfq.extract", "quote.unmet"] and quote.llm_calls == 1
    # the extraction's opening names no phrase of the mail it answers (`hook`), so the quote opens with the price
    assert quote.body.startswith("Hello Tom,\n\n") and intro not in quote.body
    assert "2 minutes" not in quote.body
    assert "260.00 EUR" in quote.body

    backup = await agent.fallback(ctx, 1)
    assert backup.body.startswith("Hello Tom,") and len(calls) == 2


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
    monkeypatch.setattr(extract, "CHECK_WAIT_S", 0.3)
    monkeypatch.setattr(unmet, "WAIT_S", 0.3)
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


AIRLINE_BODY = "Hello,\nwe can offer EUR 900.00, transit 3 days.\nRegards, Air One"
ATTACHED = {
    # the customer's invoice forwarded back, or a company letter: a labelled total that is no freight price
    "invoice_total": "Commercial invoice\nTotal value: EUR 12,500.00\n",
    # general terms: a decline word that is no decline of this request
    "terms_decline_word": "General terms\nThe carrier is unable to accept dangerous goods without a declaration.\n",
}


@pytest.mark.parametrize("case", list(ATTACHED))
async def test_an_airline_bid_keeps_its_mail_price_beside_an_unrelated_attachment(ctx, monkeypatch, case):
    class Files:
        async def attachment(self, message_id, attachment_id):
            return ATTACHED[case].encode()

    async def no_model(ctx, **kw):
        raise AssertionError("the mail alone reads by rules")

    monkeypatch.setattr(llm, "ask_json", no_model)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq().model_dump(mode="json"))
    att = Attachment(id=1, filename="terms.txt", content_type="text/plain", size=10)
    message = mail("one@air", AIRLINE_BODY).model_copy(update={"attachments": (att,)})
    await bids.record(replace(ctx, mailbox=Files()), message)
    found = [(o["option"]["total"], o["option"]["transit_days"]) for o in kinds(ctx, "option.found")]
    assert found == [("900.00", 3)]


OFFER_UNITS = [
    # airline bid with an attached per-kg breakdown: "All-in 2.80" read as a flat 2.80 EUR total
    (
        "airline_per_kg_table",
        True,
        (
            "Our rate is EUR 2.80/kg, transit 3 days, breakdown attached.\n"
            "Charges per kg (EUR)\nFreight 2.50\nFuel 0.20\nSecurity 0.10\nAll-in 2.80"
        ),
        D("280.00"),
    ),
    # trucker: a weight line under a table that names (EUR) elsewhere becomes 1410 EUR
    (
        "header_currency_on_weight",
        False,
        "Chargeable weight (kg)\nTotal: 1410\nPickup and delivery to CGN 165.70 EUR\nAll amounts (EUR) incl. fuel.",
        D("165.70"),
    ),
    # trucker: a waiting-time rate per hour labelled by PRICE_IS becomes the pickup price
    (
        "waiting_per_hour",
        False,
        (
            "Pickup at 51252 and delivery to CGN: 165.70 EUR.\n"
            "Waiting time after the first 30 minutes: our price is 45.00 EUR for every hour."
        ),
        None,
    ),
    # trucker: a per-pallet rate labelled by PRICE_IS hides the labelled total -> None -> model call
    ("per_pallet", False, "Our price is 60.00 EUR for every pallet. 3 pallets, total 180.00 EUR.", D("180.00")),
]


@pytest.mark.parametrize(("name", "airline", "body", "expected"), OFFER_UNITS, ids=[c[0] for c in OFFER_UNITS])
def test_a_rate_per_unit_or_a_weight_is_never_the_offer_total(name, airline, body, expected):
    found = bids.parse(mail("t@x", body), rfq(), needs_transit=airline)
    assert (found.total if found else None) == expected


@pytest.mark.parametrize(
    "text, price",
    [
        ("Our all-in rate is CHF 2'748.45, including fuel.", (D("2748.45"), "CHF")),  # read as CHF 2
        ("and deliver it to DUS airport for 256.85\nEUR.\nNote: the other postcode would be 28.22 EUR.",
         (D("256.85"), "EUR")),  # a pdf line wrap left the note's price alone
        ("Amount (€)\nBase charge  47.55\nTotal  124.65\nNote: the other postcode would be 74.98 €.",
         (D("124.65"), "EUR")),  # the header's sign is the total row's currency
    ],
)  # fmt: skip
def test_generated_bids_never_take_another_amount_for_the_price(text, price):
    """tools/simulate.py, family bid: each of these took a wrong figure as the offer's price."""
    assert bids._price(text, rfq()) == price


def test_two_labelled_transit_times_are_no_transit():
    text = "Note: our rate to CAI last week was EUR 900.00, with a transit time of 5 days.\nTransit time: 2 days"
    assert bids._days(text) is None  # the first one, another lane's, was quoted


@pytest.mark.parametrize(
    "text, wrong",
    [
        ("We can offer 350.00 EUR for this shipment, transit 2 days. The fuel rate is EUR 120.00, already included.",
         D("120.00")),
        ("Freight 350.00 EUR, transit 2 days.\nOur security rate is 15.00 EUR per shipment.", D("15.00")),
        ("Pickup at 51252 for 165.70 EUR.\nThe waiting rate is 45.00 EUR.", D("45.00")),
        ("Rate per kg: 2.80\nEUR 300.00 all-in, transit 2 days", D("280.00")),  # the stated all-in is 300.00
    ],
)  # fmt: skip
def test_a_surcharge_or_a_per_kg_rate_is_never_the_offer_price(text, wrong):
    price = bids._price(text, rfq())
    assert price is None or price[0] != wrong


def attached(monkeypatch, sender: str, text: str) -> Message:
    """An answer whose offer stands only in its file: the mail says "see the attachment"."""

    async def read(ctx, message):
        return f"{message.body}\n{text}", []

    monkeypatch.setattr(bids, "_with_attachments", read)
    file = Attachment(id=1, filename="offer.pdf", content_type="application/pdf", size=1)
    return mail(sender, "Please see the attached offer.").model_copy(update={"attachments": (file,)})


async def test_the_model_cross_checks_an_attached_offer_and_its_total_wins(ctx, monkeypatch):
    """Round 67: in a trucker's offer layout the rules had not seen, they read a waiting-time line (45.00 EUR) as the
    price; the model read 181.96. On any failure the rules' reading stands."""
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq(door_pickup=True).model_dump(mode="json"))
    said = {"offer": True, "total": "181.96 EUR", "components": [], "currency": "EUR", "transit_days": None,
            "eur_per_unit": None}  # fmt: skip

    async def model(ctx, **kw):
        return LlmResult(data=said, model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(llm, "ask_json", model)
    await bids.record(ctx, attached(monkeypatch, "a@truck", "Pickup EUR 45.00\nOffer total 181.96"))
    [found] = [o["option"] for o in kinds(ctx, "option.found")]
    assert (found["pickup"], kinds(ctx, "bid.crosscheck")[0]["taken"]) == ("181.96", True)

    async def down(ctx, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", down)
    await bids.record(ctx, attached(monkeypatch, "b@truck", "Pickup EUR 90.00"))
    assert [o["option"]["pickup"] for o in kinds(ctx, "option.found")] == ["181.96", "90.00"]


@pytest.mark.parametrize(
    ("text", "said", "total_eur"),
    [
        ("EUR 2.10/kg all-in, transit 3 days", {"total": "EUR 2.10", "currency": "EUR"}, "210.00"),
        ("Total: USD 1,000.00 (1 USD = 0.92 EUR), 3 days", {"total": "1000.00", "currency": "EUR"}, "920.00"),
    ],
)
async def test_a_model_total_the_text_does_not_state_for_the_shipment_never_wins(
    ctx, monkeypatch, text, said, total_eur
):
    """Review of #252: a rate per kg, or the model's currency for a converted amount, replaced the rules' total."""
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq().model_dump(mode="json"))
    data = {"offer": True, "components": [], "transit_days": "3 days", "eur_per_unit": None, **said}

    async def model(ctx, **kw):
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(llm, "ask_json", model)
    await bids.record(ctx, attached(monkeypatch, "one@air", text))
    [found] = [o["option"] for o in kinds(ctx, "option.found")]
    assert found["total_eur"] == total_eur


async def test_an_answer_the_rules_price_costs_no_model_call(ctx, monkeypatch):
    """Rounds 81 to 83: a model read of every priced answer cost a call per carrier answer and read no other total;
    an offer in the mail itself costs none."""
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq(door_pickup=True).model_dump(mode="json"))
    calls = []

    async def model(ctx, **kw):
        calls.append(kw["purpose"])
        raise RuntimeError("no model call for an answer the rules read")

    monkeypatch.setattr(llm, "ask_json", model)
    await bids.record(ctx, mail("a@truck", "Pickup EUR 165.70"))
    assert ([o["option"]["pickup"] for o in kinds(ctx, "option.found")], calls) == (["165.70"], [])


@pytest.mark.parametrize(
    ("body", "total"),
    [
        ("We can collect your shipment (total 3 euro pallets) in 50978: 165.00 EUR, pickup within 4 hours.", "165.00"),
        ("Pickup of 4 Euro pallets from 50978 to FRA: 210.00 EUR, collection within 5 hours.", "210.00"),
        ("Our price for the 2 Euro-Paletten is 145.00 EUR, pickup in 3 hours.", "145.00"),
    ],
)
def test_euro_pallets_are_no_price(body, total):
    option = bids.parse(mail("t@x", body), rfq(), needs_transit=False)
    assert option is not None and (option.total, option.currency) == (D(total), "EUR")


@pytest.mark.parametrize(
    "text, hours",
    [
        ("Pickup time: 2 days", 48),
        ("Collection within 1 working day.", 24),
        ("Lead time 2 days from order to collection", 48),
        ("Our driver can be at the pickup address 3 Tage nach Auftrag.", 72),
        ("Pickup time: 90 minutes", 2),
        ("Abholung in 30 Minuten", 1),
        ("Offer valid for 2 days. We can collect.", None),
        ("Waiting time at the pickup address after the first 30 minutes, 45.00 EUR for each hour.", None),
        ("Pickup time: 5 hours. Collection Monday to Friday, 5 days a week.", 5),
    ],
)
def test_pickup_time_in_days_or_minutes_is_counted_in_hours(text, hours):
    # A trucker without hours drops out of the balanced choice and moves the scorer's trucker (R15).
    assert bids._pickup_hours(text) == hours


async def test_a_pickup_in_another_currency_without_its_rate_takes_the_rate_a_priced_source_stated(ctx):
    # A trucker's "198.40 USD" with no rate was no price: the quote went out without the pickup (too low).
    ctx.settings = replace(ctx.settings, model_check="")
    request = rfq(door_pickup=True, pickup_postcode="12345", pickup_zone="12")
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    await bids.record(ctx, mail("a@truck", "We collect for 198.40 USD.\nPickup time: 6 hours"))
    assert not [o for o in kinds(ctx, "option.found") if o["option"].get("trucker")]
    portal = {"source": "portal", "carrier": "X", "currency": "USD", "fx_eur": "0.92"}
    events.emit(ctx, "option.found", "pricing", session_id=2, payload={"option": portal})
    await bids.record(ctx, mail("b@truck", "We collect for 198.40 USD.\nPickup time: 6 hours"))
    [found] = [o["option"] for o in kinds(ctx, "option.found") if o["option"].get("trucker")]
    assert (found["trucker"], found["currency"], found["total_eur"], found["pickup_hours"]) == (
        "Truck B", "USD", "182.53", 6)  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        "Option A air freight: 1,200.00 EUR\nOption B air freight: 1,500.00 EUR\nTransit 3 days",
        "Economy freight 1,200.00 EUR\nExpress freight 1,500.00 EUR\nTransit 3 days",
        "Air freight 1,200.00 EUR\nAir freight via DXB 1,050.00 EUR\nTransit 3 days",
        "Freight to ORD 500.00 EUR\nFreight to JFK 520.00 EUR\nTransit 3 days",
        "Last time: freight 900.00 EUR\nFreight 500.00 EUR\nFuel 80.00 EUR\n3 days",
    ],
)
def test_alternatives_and_side_prices_are_never_added_up(body):
    assert bids.parse(mail("air@x", body), rfq()) is None


def test_a_rate_the_answer_names_is_its_own_and_never_borrowed():
    """A rate of exactly 1 was unread and another carrier's 0.8301 taken instead: an answer that names its exchange
    rate in words the rules cannot read goes to the model."""
    stated = bids.parse(mail("air@x", "All-in USD 1,000.00, transit 3 days.\nExchange rate: 1 USD = 1 EUR"), rfq())
    assert (stated.total, stated.fx_eur) == (D("1000.00"), D(1))
    unread = "All-in USD 1,000.00, transit 3 days.\nExchange rate: one dollar is ninety euro cents."
    assert bids.parse(mail("air@x", unread), rfq(), known_fx=lambda currency: D("0.8301")) is None
