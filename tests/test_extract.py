import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import Attachment, LlmResult, Message, Preference
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


class Attachments:
    """The mailbox's attachment download, from files kept in memory."""

    def __init__(self, files: dict[int, bytes]):
        self.files = files

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[attachment_id]


def xlsx(rows: list[tuple]) -> bytes:
    import io

    import openpyxl

    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


NON_DANGEROUS = (
    "Please quote AAA to BBB: 3 pieces of industrial adhesives, gross weight 715 kg, total volume 2.65 cbm. "
    "The goods are general cargo, non-dangerous, and I've attached our packing specification."
)


def with_attachment(body: str, filename: str) -> Message:
    attachment = Attachment(id=1, filename=filename, content_type="application/octet-stream", size=1)
    return mail(body).model_copy(update={"attachments": (attachment,)})


async def test_a_dangerous_goods_row_in_the_packing_list_outweighs_a_mail_that_says_non_dangerous(
    conn, monkeypatch, emitted
):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    sheet = xlsx([("Packing list", None), ("Pieces:", 3), ("Dangerous goods:", "UN1133, Class 3, Adhesives", None)])
    ctx = Context(settings(), conn, Attachments({1: sheet}), None, None, None)
    rfq = await extract.extract(ctx, with_attachment(NON_DANGEROUS, "PL_53839.xlsx"), DEADLINE)
    assert rfq.dangerous_goods == "UN1133, Class 3, Adhesives"
    assert emitted[0][1]["payload"]["dangerous_goods"] == "UN1133, Class 3, Adhesives"


async def test_a_non_dangerous_request_stays_without_dangerous_goods(conn, emitted):
    sheet = xlsx([("Packing list", None), ("Dangerous goods:", "No"), ("Goods:", "Industrial adhesives")])
    ctx = Context(settings(), conn, Attachments({1: sheet}), None, None, None)
    rfq = await extract.extract(ctx, with_attachment(NON_DANGEROUS, "PL_1.xlsx"), DEADLINE)
    assert rfq.dangerous_goods is None
    assert (await extract.extract(None, mail(NON_DANGEROUS), DEADLINE)).dangerous_goods is None


async def test_an_unreadable_attachment_costs_an_error_event_never_the_request(conn, emitted):
    ctx = Context(settings(), conn, Attachments({1: b"not a workbook"}), None, None, None)
    rfq = await extract.extract(ctx, with_attachment(NON_DANGEROUS, "PL_1.xlsx"), DEADLINE)
    assert (rfq.weight_kg, rfq.dangerous_goods) == (Decimal(715), None)
    assert [kind for kind, _ in emitted] == ["error", "rfq.extracted"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Dangerous goods: UN1133, Class 3, Adhesives", "UN1133, Class 3, Adhesives"),
        ("Lithium batteries, UN 3480, Class 9, packed per PI 965", "UN 3480, Class 9, packed per PI 965"),
        ("Gefahrgut:  Klasse 3, Klebstoffe", "Klasse 3, Klebstoffe"),
        ("Dangerous goods: yes\nUN1263 paint", "UN1263 paint"),
        ("Dangerous goods: none", None),
        ("Dangerous goods: N/A", None),
        ("Gefahrgut: nein", None),
        ("Dangerous goods:  non-dangerous", None),
        ("Dangerous goods: -", None),
        ("The goods are general cargo, no dangerous goods.", None),
    ],
)
def test_only_an_explicit_declaration_is_dangerous_goods(text, expected):
    assert extract.dangerous_goods(text) == expected


@pytest.mark.asyncio
async def test_model_guessed_origin_not_in_mail_is_replaced_by_zone_airport(conn, emitted, monkeypatch):
    ctx = Context(settings=settings(), db=conn, mailbox=None, portal=None, gateway=None, board=None)
    store.save_zones(ctx, [{"zone": "21", "area": "Lueneburg", "airport": "HAM"}])
    message = mail("Please ship 300 kg with pickup from 21663 Lueneburg to Toronto.", "Quote request")
    payload = {
        "origin": "XFW", "destination": "YYZ", "weight_kg": "300", "volume_cbm": None, "pieces": None,
        "preference": "cheapest", "door_pickup": True, "pickup_postcode": "21663", "currency": "EUR",
        "language": "en", "first_name": None, "intro": None, "closing": None,
    }
    calls = []

    async def fake_ask_json(*args, **kwargs):
        calls.append(1)
        return LlmResult(data=payload, model="test", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", fake_ask_json)
    rfq = await extract.extract(ctx, message, DEADLINE)
    assert calls == [1]
    assert rfq.origin == "HAM"
    assert rfq.destination == "YYZ"


def test_destination_airport_label_with_colon_is_read_by_rules():
    message = mail(
        "Destination airport: Toronto (YYZ)\nPickup from 21663 Lueneburg, 300 kg gross.",
        "Air freight quote",
    )
    rfq = extract.by_rules(message, DEADLINE)
    assert rfq is not None
    assert rfq.origin == ""
    assert rfq.destination == "YYZ"
    assert rfq.pickup_zone == "21"
