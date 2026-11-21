import base64
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


def captured(name: str) -> Message:
    return to_message(json.loads((Path(__file__).parent / "fixtures" / "mails" / name).read_text()), "in")


async def test_a_captured_door_request_without_an_origin_takes_the_pickup_zones_airport(conn, monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("51447"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, captured("rfq-door-no-origin.json"), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.extracted_by, rfq.missing) == ("AAA", "ORD", "rules", ())


async def test_a_door_request_whose_zone_has_no_airport_is_left_to_the_model(conn, monkeypatch, emitted):
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": "FRA", "destination": "ORD", "weight_kg": "676", "volume_cbm": "5.79", "pieces": 12,
                "preference": "fastest", "door_pickup": True, "pickup_postcode": "51447", "currency": "EUR",
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    ctx = Context(settings(), conn, None, None, None, None)
    rfq = await extract.extract(ctx, captured("rfq-door-no-origin.json"), DEADLINE)
    assert (rfq.origin, rfq.extracted_by) == ("FRA", "model")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("rfq-port-to-port.json", ("DUS", "JFK", "1540", "5.90", None, False, None)),
        ("rfq-door-no-origin.json", ("", "ORD", "676", "5.79", 12, True, "51447")),
        ("rfq-door-trucking.json", ("", "JFK", "1233", "10.78", 6, True, "61018")),
        ("rfq-door-ready-for-pickup.json", ("", "ORD", "679", "4.19", 6, True, "51896")),
        # "ready for pickup", but delivered to the airport by the customer
        ("rfq-lots-two.json", ("MUC", "LAX", "998", "2.39", 2, False, None)),
        ("rfq-lots-gross-after.json", ("MUC", "CAI", "486", "2.62", 11, False, None)),
        ("rfq-door-three-lots.json", ("", "LOS", "1952", "4.6768", 13, True, "36627")),
    ],
)
def test_rules_read_every_captured_request(name, expected):
    rfq = extract.by_rules(captured(name), DEADLINE)
    actual = (
        rfq.origin,
        rfq.destination,
        rfq.weight_kg,
        rfq.volume_cbm,
        rfq.pieces,
        rfq.door_pickup,
        rfq.pickup_postcode,
    )
    origin, destination, kg, cbm, *rest = expected
    assert actual == (origin, destination, Decimal(kg), Decimal(cbm), *rest)


async def test_a_captured_request_in_lots_is_added_up_by_code_whatever_the_model_summed(conn, monkeypatch, emitted):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / "rfq-door-three-lots.json").read_text())
    message = to_message(raw, "in")
    # 730 + 4 x 48 + 5 x 206 kg gross; 1.68 + 4 x 1.1 x 1.1 x 0.52 + 5 x 1.2 x 1.0 x 0.08 cbm; 4 + 4 + 5 pieces
    assert extract._lots(extract._text(message)) == (Decimal(1952), Decimal("4.6768"), 13)

    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": None, "destination": "LOS", "weight_kg": "1750", "volume_cbm": "3.36", "pieces": 13,
                "preference": "cheapest", "door_pickup": True, "pickup_postcode": "36627", "currency": "EUR",
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("36627"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, message, DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.pieces, rfq.origin) == (Decimal(1952), Decimal("4.6768"), 13, "AAA")


def test_a_lot_whose_figures_are_not_certain_is_left_to_the_model():
    text = "Lot 1: 4 drums, 730 kg, 1.68 cbm\nLot 2: 4 cartons, each 48 kg gross (41 kg net)"
    assert extract._lots(text) is None  # no volume for lot 2
    assert extract._lots("Lot 1: 2 crates, each 50 kg, each 100 x 100 x 100 cm") is None  # one lot is no lot list


@pytest.mark.parametrize(
    "body, expected",
    [
        ("AAA to BBB, 4 cartons, each 48 kg, 0.5 cbm each.", ("192", "2.0", Preference.CHEAPEST)),
        ("AAA to BBB, 3 pallets of 250 kg per pallet, 120x80x100 cm.", ("750", "2.88", Preference.CHEAPEST)),
        ("AAA-BBB, 500 kg, 2 cbm. Not urgent, no rush, the cheapest please.", ("500", "2", Preference.CHEAPEST)),
        ("AAA-BBB:\n- 4 cartons, each 48 kg, 110 x 110 x 52 cm\n- 2 pallets, together 400 kg, 2.1 cbm",
         ("592", "4.6168", Preference.CHEAPEST)),
        ("AAA nach BBB: 3 Kartons à 30 kg, je 60x40x50 cm. Bitte schnellstmöglich.", ("90", "0.360", Preference.FASTEST)),
    ],
)  # fmt: skip
def test_rules_count_per_piece_figures_and_read_wishes(body, expected):
    rfq = extract.by_rules(mail(body), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.preference) == (Decimal(expected[0]), Decimal(expected[1]), expected[2])


def test_a_volume_the_rules_cannot_settle_is_left_to_the_model():
    body = "AAA-BBB, total 500 kg: 2 pallets 120x80x150 cm and 3 boxes 60x40x40 cm."
    assert extract.by_rules(mail(body), DEADLINE) is None  # volume 0 would under-price the chargeable weight


def test_a_lot_line_code_cannot_read_leaves_the_whole_list_to_the_model():
    text = "Lot 1: 4 drums, together 730 kg, together 1.68 cbm\nLot 2: 4 cartons, each 48 kg, each 110 x 110 x 52 cm\n"
    assert extract._lots(text + "Lot 3: one crate, 900 kg gross, 2 cbm") is None  # summing two lots: 922 kg, not 1822
    assert extract._lots(text.replace("Lot 1:", "-").replace("Lot 2:", "-") + "- 1x crate, 900 kg, 2 cbm") is None


class FixtureMailbox:
    def __init__(self, raw: dict):
        self.files = {a["id"]: base64.b64decode(a["content_base64"]) for a in raw["attachments"]}

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[attachment_id]


@pytest.mark.parametrize(
    "name, declared",
    [
        ("rfq-dg-packing-list.json", ("UN1133, Class 3, Adhesives", "PL_53839.xlsx")),
        ("rfq-dg-instruction-bullets.json", ("UN1950, Class 2.1, Aerosols, flammable", "packing-spec_33955.pdf")),
        ("rfq-door-trucking.json", None),
    ],
)
async def test_dangerous_goods_declared_in_an_attachment_count_although_the_mail_says_non_dangerous(name, declared):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / name).read_text())
    ctx = Context(settings(), None, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    assert await extract.dangerous_goods(ctx, to_message(raw, "in")) == declared


async def test_a_dangerous_goods_request_lists_the_documents_as_missing(conn, emitted):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / "rfq-dg-packing-list.json").read_text())
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    store.save_zones(ctx, [{"zone": zone_of("61391"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, to_message(raw, "in"), DEADLINE)
    assert (rfq.origin, rfq.weight_kg, rfq.missing) == ("AAA", Decimal(715), (extract.DG_DOCUMENTS,))
    assert ("rfq.dangerous_goods", {"declared": "UN1133, Class 3, Adhesives", "source": "PL_53839.xlsx"}) in [
        (kind, kw["payload"]) for kind, kw in emitted
    ]
