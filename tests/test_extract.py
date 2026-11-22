import base64
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import Attachment, LlmResult, Message, Preference
from pixel.core import events, outbox, poller
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import calc, store
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


def test_delivery_legs_around_the_flight_do_not_turn_the_lane():
    body = ("From AAA airport to Town, 350 kg, 1.4 cbm. We deliver to AAA ourselves.\n"
            "Please include delivery from BBB airport to the door.")  # fmt: skip
    rfq = extract.by_rules(mail(body), DEADLINE)
    assert (rfq.origin, rfq.destination) == ("AAA", "BBB")
    # Round 154: "from Munich Airport (MUC) to RUH ... we will deliver them to MUC ourselves" went to the model.
    rfq = extract.by_rules(mail("Please quote from Town Airport (AAA) to BBB, 631 kg. We will deliver them to AAA ourselves."), DEADLINE)
    assert (rfq.origin, rfq.destination) == ("AAA", "BBB")


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


async def test_the_model_cross_checks_the_rules_and_its_wish_wins(conn, monkeypatch, emitted):
    """Rounds 33 to 35: the rules took the mail's figure over the attached packing list's; gpt-4.1 read the list's.
    Without an attachment it reads nothing: on every request it cost about 0.0025 USD (rounds 81 to 83)."""
    ctx = Context(settings(), conn, None, None, None, None)
    said = {"origin": "AAA", "destination": "CCC", "weight_kg": "480", "volume_cbm": None, "pieces": None,
            "lots": [], "preference": "fastest", "door_pickup": False, "pickup_postcode": None, "currency": None,
            "language": "en", "first_name": None, "intro": None, "closing": None}  # fmt: skip

    async def model(ctx, **kw):
        assert kw["purpose"] == "rfq.crosscheck"
        return LlmResult(data=said, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    async def attachments(ctx, message):
        return [("packing.pdf", ["Packing list", "The packing list says 480."])]

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(extract, "_attachments", attachments)
    packing = Attachment(id=1, filename="packing.pdf", content_type="application/pdf", size=1)
    packed = mail("AAA → BBB, 500 kg, 2 cbm.").model_copy(update={"attachments": (packing,)})
    rfq = await extract.extract(ctx, packed, DEADLINE)
    assert (rfq.extracted_by, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == ("rules", "BBB", 500, 2)
    assert rfq.preference == Preference.FASTEST  # the wish wins as read; 480 stands without its unit

    calls = []

    async def boom(ctx, **kw):
        calls.append(kw["purpose"])
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", boom)
    assert (await extract.extract(ctx, packed, DEADLINE)).weight_kg == 500
    assert (await extract.extract(ctx, mail("AAA → BBB, 500 kg"), DEADLINE)).weight_kg == 500
    assert calls == ["rfq.crosscheck"]


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


async def test_a_captured_door_request_takes_the_zones_airport_over_an_origin_no_zone_trucks_to(
    conn, monkeypatch, emitted
):
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": "XFW", "destination": "YYZ", "weight_kg": "855", "volume_cbm": "3.74", "pieces": 4,
                "preference": "cheapest", "door_pickup": True, "pickup_postcode": "21663", "currency": "EUR",
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    monkeypatch.setattr(extract, "by_rules", lambda message, deadline: None)  # the rules read this mail since
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("21663"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, captured("rfq-door-model-names-unserved-origin.json"), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.extracted_by, rfq.missing) == ("AAA", "YYZ", "model", ())

    # Round 14: CGN is a zone's airport too, yet not the pickup zone's, and no airline flew CGN-PEK.
    store.save_zones(ctx, [{"zone": "00", "area": "Elsewhere", "airport": "XFW"}])
    rfq = await extract.extract(ctx, captured("rfq-door-model-names-unserved-origin.json"), DEADLINE)
    assert rfq.origin == "AAA"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("rfq-port-to-port.json", ("DUS", "JFK", "1540", "5.90", 1, False, None)),
        ("rfq-door-no-origin.json", ("", "ORD", "676", "5.79", 12, True, "51447")),
        ("rfq-door-trucking.json", ("", "JFK", "1233", "10.78", 6, True, "61018")),
        ("rfq-door-ready-for-pickup.json", ("", "ORD", "679", "4.19", 6, True, "51896")),
        # "ready for pickup", but delivered to the airport by the customer
        ("rfq-lots-two.json", ("MUC", "LAX", "998", "2.39", 2, False, None)),
        ("rfq-lots-gross-after.json", ("MUC", "CAI", "486", "2.62", 11, False, None)),
        ("rfq-import-no-pickup.json", ("FRA", "DOH", "102", "0.49", 6, False, None)),
        ("rfq-door-three-lots.json", ("", "LOS", "1952", "4.6768", 13, True, "36627")),
        # labelled bullets: "- Destination airport: Toronto (YYZ), CA", "- Pickup: from our site at 21663 Lueneburg"
        ("rfq-dg-instruction-bullets.json", ("", "YYZ", "855", "3.74", 4, True, "21663")),
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
        ("AAA-BBB:\n- 2 pallets, 120 x 80 x 60 cm, 210 kg each\n- 1 crate, 0.5 cbm, 180 kg\n- 3 boxes, 40 kg, 0.1 cbm each",
         ("640", "1.952", Preference.CHEAPEST)),
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


async def test_facts_only_in_a_file_no_code_reads_are_missing_without_an_error(emitted):
    """A `.doc` packing list (no code reads it) is an `attachment.unreadable`, not an
    `error`: the mail goes on without it, so its facts are missing and the one question asks for them."""
    ctx = Context(settings(), None, None, None, None, None)
    ctx.mailbox = FixtureMailbox({"attachments": []})  # a type without a reader is never downloaded
    sheet = Attachment(id=1, filename="packing-list.doc", content_type="application/msword", size=10)
    message = mail("Please quote, all details in the packing list.").model_copy(update={"attachments": (sheet,)})
    assert await extract._attachments(ctx, message) == []
    assert [(kind, kw["payload"]) for kind, kw in emitted] == [
        (
            "attachment.unreadable",
            {"file": "packing-list.doc", "type": "application/msword", "reason": "no code parser for .doc"},
        )
    ]


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


@pytest.mark.parametrize(
    "line, declared",
    [
        ("Please quote 300 kg of non-dangerous goods, AAA to BBB.", None),
        ("General cargo, no dangerous goods.", None),
        ("Keine Gefahrgut-Sendung, bitte AAA nach BBB.", None),
        ("Dangerous goods: UN1133, Class 3", "UN1133, Class 3"),
        ("Dangerous goods | Adhesives, Class 3", "Adhesives, Class 3"),
    ],
)
def test_only_a_declaration_is_dangerous_goods(line, declared):
    assert extract._declared([line]) == declared


async def test_the_language_is_codes_reading_not_the_models(monkeypatch, emitted):
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": "AAA", "destination": "BBB", "weight_kg": "300", "volume_cbm": None, "pieces": None,
                "preference": "cheapest", "door_pickup": False, "pickup_postcode": None, "currency": None,
                "language": "de"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    body = "Hello, please quote the shipment from our site in Lueneburg to Toronto, about three hundred kilos. Thanks"
    assert (await extract.extract(None, mail(body), DEADLINE)).language == "en"


def test_no_pickup_needed_is_no_door_pickup():
    rfq = extract.by_rules(mail("AAA to BBB, 300 kg, 1 cbm. No pickup needed, the goods are at the airport."), DEADLINE)
    assert rfq.door_pickup is False


@pytest.mark.parametrize(
    "text",
    ["Pickup from our site in 61391, no pickup before 9:00 please.", "Abholung in 61391, keine Abholung am Freitag."],
)
def test_a_pickup_time_window_keeps_the_door_pickup(text):
    assert extract._pickup(text) == (True, "61391")


def test_exw_with_an_address_is_a_door_pickup():
    assert extract._pickup("Sold on EXW terms, ex works our plant at Hauptstr. 1, 61391 Town.") == (True, "61391")


def lot(pieces, weight, per_piece=False, unit="kg", cbm=None, dims=None):
    return {"pieces": pieces, "weight": weight, "weight_unit": unit, "weight_per_piece": per_piece,
            "volume_cbm": cbm, "volume_per_piece": False, "dimensions": dims}  # fmt: skip


@pytest.mark.parametrize(
    "weight_kg, lots, expected",
    [
        # as gpt-4.1-mini read the three-lot mail without its "Lot" labels: its own total was 1094 kg
        ("1094", [lot(4, "730", cbm="1.68"), lot(4, "48", True, dims="110 x 110 x 52 cm"),
                  lot(5, "206", True, dims="120 x 100 x 8 cm")], ("1952", "4.6768", 13)),
        (None, [lot(3, "400", True, "lb", dims="120x80x110 cm")], (str(1200 * extract.LB), "3.168", 3)),
        ("676", [lot(12, "600", cbm="5.79")], ("676", "5.79", 12)),  # one group beside a total in kg: the total
    ],
)  # fmt: skip
async def test_code_adds_up_the_groups_the_model_copies(monkeypatch, emitted, weight_kg, lots, expected):
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": "AAA", "destination": "BBB", "weight_kg": weight_kg, "volume_cbm": "5.79", "pieces": 12,
                "lots": lots, "preference": "cheapest", "door_pickup": False, "pickup_postcode": None,
                "currency": None, "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    rfq = await extract.extract(None, mail("goods as listed, 676 kg in total"), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.pieces, rfq.missing) == (
        Decimal(expected[0]), Decimal(expected[1]), expected[2], ()
    )  # fmt: skip


@pytest.mark.parametrize(
    "wish, fastest",
    [
        ("Speed matters more than price.", True),
        ("Please ship it as quickly as possible.", True),
        ("This is time-critical.", True),
        ("Price matters more than speed, no rush.", False),
        ("The cheapest option please, ideally urgent.", False),
        ("Best regards, Nordwind Express GmbH", False),
        ("Bitte so schnell wie möglich.", True),
        ("Price is secondary, transit time is key.", True),
        ("Bitte die kürzeste Laufzeit.", True),
        ("We need the shortest lead time, please go for express.", True),
        ("The shortest transit we had was 3 days, but price is what counts now.", False),
        ("Not the shortest transit, the best rate. Not time-sensitive.", False),
        ("This is our first flight shipment with you.", False),
        ("", False),
    ],
)
def test_the_wish_is_read_from_its_wording(wish, fastest):
    rfq = extract.by_rules(mail(f"AAA to BBB, 300 kg, 1 cbm. {wish}"), DEADLINE)
    assert (rfq.preference == Preference.FASTEST) is fastest


async def test_a_pickup_named_by_its_town_takes_that_zone_and_its_airport(conn, monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "80", "area": "Munich", "airport": "AAA"},
                           {"zone": "81", "area": "Munich South", "airport": "AAA"},
                           {"zone": "86", "area": "Augsburg", "airport": "AAA"}])  # fmt: skip
    body = "Please quote the cheapest option to BBB, 300 kg, 1 cbm. Pickup from our warehouse in Munich South."
    rfq = await extract.extract(ctx, mail(body + "\nRegards, Anna"), DEADLINE)
    assert (rfq.origin, rfq.pickup_zone, rfq.pickup_postcode, rfq.missing) == ("AAA", "81", None, ())


async def test_the_pickup_lines_town_wins_over_the_signatures(conn, emitted):
    message = captured("rfq-door-import-two-towns.json")  # "at our site in 24983 (Kiel)", "greetings from Regensburg"
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "24", "area": "Kiel", "airport": "AAA"},
                           {"zone": "93", "area": "Regensburg", "airport": "CCC"}])  # fmt: skip
    rfq = extract.by_rules(message.model_copy(update={"body": message.body.replace("24983 ", "")}), DEADLINE)
    rfq = extract._town_zone(ctx, rfq, extract._text(message))
    assert rfq.pickup_zone == "24"  # the pickup line's town
    assert (await extract.extract(ctx, message, DEADLINE)).origin == "AAA"


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("Tüm masraflar dahil EUR cinsinden fiyat verebilir misiniz?", "EUR"),  # round 126
        ("Our budget is 3000 EUR.", None),
        ("Goods value EUR 45,000 for insurance.", None),
        ("Value 12.500,00 EUR, please quote in CHF.", "CHF"),
    ],
)
def test_the_one_code_named_away_from_a_figure_is_the_currency(text, currency):
    assert extract._currency(text) == currency


@pytest.mark.parametrize(
    "text",
    [
        "Please quote the cheapest option, it does not need to be the fastest.",
        "We are looking for the cheapest, not the fastest routing.",
        "Bitte das günstigste Angebot, muss nicht das schnellste sein.",
        "Could you quickly send me a price for 300 kg?",
    ],
)
def test_a_negated_superlative_or_a_quick_reply_is_no_wish_for_speed(text):
    assert extract._preference(text) == Preference.CHEAPEST


@pytest.mark.parametrize(
    "body, kg",
    [
        ("AAA to BBB: 1 pallet, gross weight: 480 lbs, 120x80x100 cm.", Decimal("217.72")),  # not 480 kg; two decimals as the scorer
        ("AAA to BBB, 3 pallets each 400 lbs, 120x80x110 cm", 3 * Decimal("181.44")),
        ("AAA-BBB 1.2t 6cbm asap", Decimal(1200)),
        ("AAA-BBB, 2,5 Tonnen, 10 cbm", Decimal(2500)),
        ("AAA-BBB 1000 kg (2205 lbs), 3 cbm", Decimal(1000)),
        ("AAA-BBB, 500 t-shirts, 300 kg, 2 cbm", Decimal(300)),
    ],
)
def test_code_converts_pounds_and_tonnes(body, kg):
    assert extract.by_rules(mail(body), DEADLINE).weight_kg == kg


async def test_a_gateway_that_refuses_the_model_still_gives_a_reading(monkeypatch, emitted):
    async def refused(ctx, **kw):
        raise RuntimeError("model not allowed")

    monkeypatch.setattr(llm, "ask_json", refused)
    body = "AAA to BBB, 2 boxes: one 40 kg, one 60 kg, 100 kg in total, 0.3 cbm and 0.5 cbm"
    rfq = await extract.extract(None, mail(body), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.extracted_by) == (Decimal(100), Decimal("0.5"), "guess")
    assert emitted[0][1]["payload"]["where"] == "rfq_extract_model"
    assert "model not allowed" not in str(emitted[0][1]["payload"])  # the gateway's text can quote the OpenAI key
    with pytest.raises(RuntimeError):  # nothing to guess from
        await extract.extract(None, mail("Could you help us with a shipment next week?"), DEADLINE)


def test_an_older_mail_quoted_below_the_request_is_not_read():
    message = captured("rfq-door-quoted-old-request.json")  # "RE: FW: ZURICH AIRPORT - MEL / 87 PLT 3,459 KGS"
    rfq = extract.by_rules(message, DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm, rfq.pickup_postcode) == (
        "", "PEK", Decimal(1081), Decimal("8.03"), "42401"
    )  # fmt: skip


async def test_facts_given_only_in_the_attached_packing_list_are_read_from_it(conn, monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "mails" / "synthetic-rfq-facts-in-attachment.json").read_text()
    )
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    store.save_zones(ctx, [{"zone": zone_of("61391"), "area": "Town", "airport": "AAA"}])
    rfq = await extract.extract(ctx, to_message(raw, "in"), DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == ("AAA", "GRU", Decimal(715), Decimal("2.65"))
    assert extract.DG_DOCUMENTS in rfq.missing
    assert ("rfq.dangerous_goods", {"declared": "UN1133, Class 3, Adhesives", "source": "PL_53839.xlsx"}) in [
        (kind, kw["payload"]) for kind, kw in emitted
    ]


async def test_a_request_sent_as_a_voice_message_is_read_by_rules_from_its_transcript(conn, monkeypatch):
    """Round 65: the whole request was a voice message. Unread, we had to ask the customer for the facts."""
    from pixel.agents.tariffs import agent as tariffs

    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    offers = json.loads((Path(__file__).parent / "fixtures" / "pickup-offer-layouts.json").read_text())
    monkeypatch.setattr(llm, "ask_json", boom)
    monkeypatch.setattr(tariffs, "_transcribe", lambda content: offers["request_transcript"])

    class Recording:
        async def attachment(self, message_id, attachment_id):
            return b"ID3"

    ctx = Context(settings(), conn, Recording(), None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("51943"), "area": "Town", "airport": "AAA"}])
    message = Message(id=1, session_id=1, direction="in", sender="c@x", to="us@x", subject="Voice message",
                      body="Please listen to the attached recording.", sent_at=datetime(2026, 11, 21, tzinfo=UTC),
                      attachments=[Attachment(id=0, filename="voice-message.mp3", content_type="audio/mpeg", size=3)])  # fmt: skip
    rfq = await extract.extract(ctx, message, DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm, rfq.pieces, rfq.pickup_postcode) == (
        "AAA", "LIM", Decimal(1804), Decimal("17.18"), 11, "51943")  # fmt: skip


async def test_a_packing_spec_whose_table_bears_out_its_volume_outweighs_the_mail(conn, monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    # Round 33: the mail says 2.78 cbm (120 x 40 x 116 cm), the attached spec 1.92 cbm and a table row of
    # 5 pieces of 40 x 120 x 80 cm. The scorer priced 1.92: our 1475.00 on 464.26 kg was rejected as too high.
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "mails" / "rfq-sli-volume-differs-from-mail.json").read_text()
    )
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    rfq = await extract.extract(ctx, to_message(raw, "in"), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.pieces) == (Decimal(292), Decimal("1.92"), 5)
    evidence = next(kw["payload"]["sources"] for kind, kw in emitted if kind == "rfq.evidence")
    assert evidence["volume_cbm"] == ["SLI_53351.pdf"]


async def test_a_packing_spec_whose_table_bears_out_its_gross_weight_outweighs_the_mail(conn, emitted):
    # Round 35: the mail says 2644 kg, the attached spec's total and its one row 1751 kg (19 x 100 x 63 x 100 cm,
    # 11.97 cbm). On the mail's weight we billed 2644 kg instead of 1998.99 and were rejected as too high.
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "mails" / "rfq-spec-weight-differs-from-mail.json").read_text()
    )
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    rfq = await extract.extract(ctx, to_message(raw, "in"), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.pieces) == (Decimal(1751), Decimal("11.97"), 19)
    evidence = next(kw["payload"]["sources"] for kind, kw in emitted if kind == "rfq.evidence")
    assert evidence["weight_kg"] == ["shipment_details_81971.xlsx"]


@pytest.mark.parametrize(
    "fixture, instruction, field, value",
    [
        (
            "rfq-packing-list-volume-differs-from-mail.json",
            "The packing list is outdated. Use the dimensions and gross weight in this email.",
            "volume_cbm",
            "11.69",
        ),
        (
            "rfq-spec-weight-differs-from-mail.json",
            "Please use the gross weight in this email.",
            "weight_kg",
            "2644",
        ),
        (
            "rfq-spec-weight-differs-from-mail.json",
            "Corrected gross weight: 2800 kg.",
            "weight_kg",
            "2800",
        ),
    ],
)
async def test_explicit_email_corrections_take_precedence(conn, emitted, fixture, instruction, field, value):
    raw = json.loads((Path(__file__).parent / "fixtures" / "mails" / fixture).read_bytes())
    raw["body"] = f"{instruction}\n\n{raw['body']}"
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = FixtureMailbox(raw)
    rfq = await extract.extract(ctx, to_message(raw, "in"), DEADLINE)
    assert getattr(rfq, field) == Decimal(value)
    evidence = next(kw["payload"]["sources"] for kind, kw in emitted if kind == "rfq.evidence")
    assert evidence[field] == ["mail"]


@pytest.mark.parametrize(
    "instruction, volume, source",
    [
        ("", "3.20", "mail"),
        ("Please use the revised packing list in revised.pdf.", "2.40", "revised.pdf"),
        ("Do not use revised.pdf.", "3.20", "mail"),
        ("Please use different.pdf.", "3.20", "mail"),
    ],
)
async def test_packing_documents_must_match_the_lane_and_a_named_revision_resolves_the_volume(
    conn, monkeypatch, emitted, instruction, volume, source
):
    async def attachments(ctx, message):
        header = "Length (cm) Width (cm) Height (cm) Pieces"
        different = ("different.pdf", ["Destination airport: CCC", "Total volume (cbm): 1.60", header, "100 100 80 2"])
        if not instruction:
            return [different]
        return [
            ("original.pdf", ["Destination airport: BBB", "Total volume (cbm): 2.00", header, "100 100 100 2"]),
            ("revised.pdf", ["Destination airport: BBB", "Total volume (cbm): 2.40", header, "100 100 120 2"]),
            different,
        ]

    monkeypatch.setattr(extract, "_attachments", attachments)
    ctx = Context(settings(), conn, None, None, None, None)
    message = mail(f"{instruction}\nAAA to BBB, 2 pieces, 240 kg gross, 3.20 cbm.")
    message = message.model_copy(
        update={"attachments": (Attachment(id=1, filename="packing.pdf", content_type="application/pdf", size=1),)}
    )
    rfq, text = await extract.read_request(ctx, message, DEADLINE)
    assert (rfq.destination, rfq.volume_cbm) == ("BBB", Decimal(volume))
    evidence = next(kw["payload"] for kind, kw in emitted if kind == "rfq.evidence")
    assert evidence["sources"]["volume_cbm"] == [source]
    assert evidence["attachment_route_conflicts"] == ["different.pdf"]
    assert "Destination airport: CCC" not in text


TABLE = "Length (cm) Width (cm) Height (cm) Pieces {column}\n100 100 63 19 {row}"


@pytest.mark.parametrize(
    "document, kg",
    [
        ("Total gross weight (kg): 1751\n" + TABLE.format(column="Gross weight (kg)", row="1751"), 1751),
        ("Total gross weight: 1,751 kg\n" + TABLE.format(column="Gross weight (kgs)", row="1,751"), 1751),
        ("Total gross weight (kg): 1751", 1751),  # no table
        (TABLE.format(column="Gross weight (kg)", row="1751"), 1751),  # no total line
        (TABLE.format(column="Gewicht je Stück (kg)", row="92"), 1748),  # per piece, "Stück" no count column
    ],
)
def test_a_document_states_its_weight_in_a_total_its_table_or_both(document, kg):
    assert extract._document_facts(document)["weight_kg"] == kg


@pytest.mark.parametrize(
    "document",
    [
        "Total gross weight (kg): 1751\n" + TABLE.format(column="Gross weight (lbs)", row="1751"),
        "Total gross weight (kg): 1751\n" + TABLE.format(column="Gross weight (kg)", row="92"),  # per piece
    ],
)
def test_a_document_whose_total_and_table_disagree_settles_nothing(document):
    assert extract._document_facts(document) is None


@pytest.mark.parametrize(
    "body, model_kg, model_cbm, pieces, expected",
    [
        ("Hi, 1 pallet 480 lbs, 120x80x100 cm, AAA area to BBB.", "217.7", "0.96", 1, (Decimal("217.72"), "0.96")),
        ("Two cartons of 60 x 40 x 50 cm, 95 kg together, to BBB.", "95", "0.25", 2, ("95", "0.240")),
    ],
)
async def test_a_number_the_mail_does_not_write_is_computed_by_code(monkeypatch, emitted, body, model_kg, model_cbm,
                                                                    pieces, expected):  # fmt: skip
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": "AAA", "destination": "BBB", "weight_kg": model_kg, "volume_cbm": model_cbm,
                "pieces": pieces, "lots": [], "preference": "cheapest", "door_pickup": False,
                "pickup_postcode": None, "currency": None, "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    rfq = await extract.extract(None, mail(body), DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm) == (Decimal(expected[0]), Decimal(expected[1]))


async def test_a_pickup_with_no_place_takes_the_site_this_customer_named_before(conn, monkeypatch):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "36", "area": "Fulda", "airport": "AAA"}])
    earlier = captured("rfq-door-three-lots.json")  # round 9: "Pickup is at our site in 36627 (Fulda)"
    await extract.extract(ctx, earlier, DEADLINE)
    body = (
        "Could you quote the fastest air freight option to PVG, including truck pickup from our warehouse to the "
        "airport? 11 pieces, 349 kg gross, 2.40 cbm."
    )  # fmt: skip  round 16, same customer
    later = mail(body).model_copy(update={"id": 3937, "session_id": 3937, "sender": earlier.sender})
    rfq = await extract.extract(ctx, later, DEADLINE)
    assert (rfq.origin, rfq.pickup_postcode, rfq.pickup_zone, rfq.extracted_by) == ("AAA", "36627", "36", "rules")


async def test_of_several_earlier_sites_the_first_counts(conn, monkeypatch):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "51", "area": "Bergisch Gladbach", "airport": "CCC"},
                           {"zone": "21", "area": "Lueneburg", "airport": "AAA"}])  # fmt: skip
    for name in ("rfq-door-no-origin.json", "rfq-dg-instruction-bullets.json"):  # Lindqvist: 51447, then 21663
        await extract.extract(ctx, captured(name).model_copy(update={"attachments": []}), DEADLINE)
    rfq = await extract.extract(ctx, captured("rfq-door-warehouse-earlier-sites.json"), DEADLINE)
    # Round 18: no airline and trucker from HAM (21663, the latest site) came under the scorer's best.
    assert (rfq.origin, rfq.destination, rfq.pickup_postcode, rfq.extracted_by) == ("CCC", "EZE", "51447", "rules")


async def test_a_door_request_whose_customer_named_sites_in_two_zones_asks_and_reads_the_answer(conn, monkeypatch):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "51", "area": "Bergisch Gladbach", "airport": "CCC"},
                           {"zone": "21", "area": "Lueneburg", "airport": "AAA"}])  # fmt: skip
    for name in ("rfq-door-no-origin.json", "rfq-dg-instruction-bullets.json"):  # Lindqvist: 51447, then 21663
        await extract.extract(ctx, captured(name).model_copy(update={"attachments": []}), DEADLINE)
    request, answer = captured("rfq-door-warehouse-two-sites.json"), captured("customer-names-pickup.json")
    asked = []

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        asked.append((to, subject, body, purpose))
        poller.ingest(ctx, answer)  # "The pickup address is 51115 Bergisch Gladbach", in the request's thread
        return answer.model_copy(update={"id": answer.id - 1, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    request = poller.ingest(ctx, request)
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    ((to, subject, body, purpose),) = asked
    assert (to, subject, purpose) == (request.sender, "Re: Air freight quote to SGN", "question")
    assert "postcode" in body and not re.search(r"\d", body)  # a question, never a price
    assert (rfq.origin, rfq.pickup_postcode, rfq.pickup_zone, rfq.extracted_by) == ("CCC", "51115", "51", "rules")


async def test_a_customer_with_one_known_site_is_still_asked_and_a_town_answer_gives_the_zone(conn, monkeypatch):
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "61", "area": "Bad Homburg", "airport": "CCC"},
                           {"zone": "86", "area": "Augsburg", "airport": "AAA"}])  # fmt: skip
    await extract.extract(ctx, captured("rfq-dg-packing-list.json").model_copy(update={"attachments": []}), DEADLINE)
    request, answer = captured("rfq-door-warehouse-wrong-site.json"), captured("customer-names-pickup-town.json")

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        poller.ingest(ctx, answer)  # "The pickup is in Augsburg, Germany."
        return answer.model_copy(update={"id": answer.id - 2, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    request = poller.ingest(ctx, request)  # round 20: Weidner had named 61391 before, which was wrong
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert (rfq.origin, rfq.pickup_zone, rfq.pickup_postcode, rfq.extracted_by) == ("AAA", "86", None, "rules")


VARIANTS = json.loads((Path(__file__).parent / "fixtures" / "request-variants.json").read_text())


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(v, marks=pytest.mark.xfail(strict=True, reason=v["hole"])) if "hole" in v else v
        for v in VARIANTS["variants"]
    ],
    ids=lambda v: v["name"],
)
def test_one_request_written_many_ways(variant):
    # Right or None (the model reads it); never a confident wrong read.
    rfq = extract.by_rules(mail(variant["body"]), DEADLINE)
    if rfq is None:
        return
    want = {**VARIANTS["base"], **variant.get("expect", {})}
    got = {k: getattr(rfq, k) for k in want}
    got["preference"] = got["preference"].value
    assert got == {
        **want,
        "weight_kg": Decimal(want["weight_kg"]),
        "volume_cbm": Decimal(want["volume_cbm"]),
    }


@pytest.mark.parametrize(
    "body, cbm, pieces",
    [
        ("AAA to BBB, 2,400 lbs, 5 skids 48x40x50 in, cheapest.", 5 * 48 * 40 * 50 * Decimal("0.0254") ** 3, 5),
        ("AAA to BBB, 4 pallets, 800 kg, 24 x 16 x 51 in each.", 4 * 24 * 16 * 51 * Decimal("0.0254") ** 3, 4),
        ("AAA nach BBB: 6 Packstücke, 300 kg, je 120 x 80 x 100 cm.", Decimal("5.76"), 6),
        ("AAA to BBB, 1 piece, 300 kg, 1200 x 800 x 1000 mm.", Decimal("0.96"), 1),
    ],
)
def test_dimensions_in_inches_and_more_piece_words(body, cbm, pieces):
    rfq = extract.by_rules(mail(body), DEADLINE)
    assert (round(rfq.volume_cbm, 6), rfq.pieces) == (round(cbm, 6), pieces)


async def test_the_answer_gives_the_place_also_when_the_model_read_the_request(conn, monkeypatch):
    async def as_the_model_read_it(ctx, **kw):
        data = {"origin": None, "destination": "SGN", "weight_kg": "874", "volume_cbm": "1.92", "pieces": 2,
                "lots": [], "preference": "cheapest", "door_pickup": True, "pickup_postcode": None,
                "currency": "EUR", "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "51", "area": "Bergisch Gladbach", "airport": "CCC"}])
    answer = captured("customer-names-pickup.json")
    answer = answer.model_copy(update={"body": "Hi Lena,\n\nSorry, it is 51115.\n\nSara"})

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        poller.ingest(ctx, answer)
        return answer.model_copy(update={"id": answer.id - 1, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    body = "Please quote SGN with truck pickup from our warehouse. 874 kg gross, two crates of 120x100x80 cm."
    request = mail(body).model_copy(update={"id": 4370, "session_id": 4370, "sender": answer.sender})
    request = poller.ingest(ctx, request)  # the dimensions without a volume send it to the model
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert (rfq.origin, rfq.pickup_postcode, rfq.pickup_zone, rfq.extracted_by) == ("CCC", "51115", "51", "model")


def test_the_origin_airport_named_after_to_is_no_destination():
    body = "Pickup from our site in 60311 Frankfurt, our truck goes to departure airport FRA. Air freight to New York."
    assert extract._destination_only(body + " 300 kg, 1 cbm.") is None  # FRA is the origin; the model reads the rest


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("FRA-ORD 340 kg per kg rate pls, 1.2 cbm", "en"),
        ("Origin MUC, destination GRU, 715 kg, 2.65 cbm, ready 2026-11-25, Saludos, Klaus", "en"),
        ("Hola, necesitamos una cotización de FRA a GRU, 715 kg. Gracias", "es"),
    ],
)
def test_one_word_of_a_language_we_translate_into_does_not_make_an_english_mail_foreign(text, language):
    assert extract._language(text) == language


async def test_an_answer_that_gives_every_fact_is_read_after_the_model_could_read_nothing(conn, monkeypatch):
    """Round 69: the recording could not be read, we asked, the customer answered with everything. "Select the
    departure airport for the fastest option to JFK" read JFK as the origin, so the answer was dropped: no quote."""

    async def nothing_read(ctx, **kw):
        data = {k: None for k in extract.SCHEMA["required"]} | {"lots": [], "preference": "cheapest",
                                                                "door_pickup": False, "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    class Unreadable:
        async def attachment(self, message_id, attachment_id):
            raise ValueError

    monkeypatch.setattr(llm, "ask_json", nothing_read)
    ctx = Context(settings(), conn, Unreadable(), None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("42925"), "area": "Town", "airport": "AAA"}])
    request, answer = captured("rfq-voice-unread.json"), captured("customer-answers-every-fact.json")

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        poller.ingest(ctx, answer)
        return answer.model_copy(update={"id": answer.id - 1, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    request = poller.ingest(ctx, request)
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm, rfq.pickup_postcode, rfq.preference) == (
        "AAA", "JFK", Decimal(959), Decimal("8.04"), "42925", Preference.FASTEST)  # fmt: skip


async def test_the_asked_facts_come_from_the_answer_alone_when_the_rules_cannot_read_the_request(conn, monkeypatch):
    async def only_the_new_destination(ctx, **kw):
        data = {k: None for k in extract.SCHEMA["required"]} | {"destination": "BBB", "lots": [],
                "preference": "fastest", "door_pickup": False, "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", only_the_new_destination)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": zone_of("40043"), "area": "Town", "airport": "AAA"}])
    body = ('Could you please quote the same shipment as in my request "Air freight quote request to CCC", sent on '
            "2026-11-21 for CCC? The new destination is BBB. Please send your all-in price for the fastest option.")  # fmt: skip
    request = poller.ingest(ctx, mail(body).model_copy(update={"id": 9200, "session_id": 9200}))

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        poller.ingest(ctx, request.model_copy(update={"id": 9202, "in_reply_to": 9201, "references": (9200, 9201),
            "body": "The departure airport has not been specified. Please arrange pickup from our site in postcode "
                    "40043, Germany. The shipment has a gross weight of 585 kg and a total volume of 5.01 m³."}))  # fmt: skip
        return request.model_copy(update={"id": 9201, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm, rfq.pickup_postcode, rfq.missing) == (
        "AAA", "BBB", Decimal(585), Decimal("5.01"), "40043", ())  # fmt: skip


def test_in_before_a_word_is_no_inch():
    assert extract._volume("3 pallets 120x80x150 in total, 900 kg", 3) == Decimal("4.32")  # not 70.8 cbm in inches


async def test_a_request_without_a_volume_asks_for_it_and_reads_the_answer(conn, monkeypatch):
    ctx = Context(settings(), conn, None, None, None, None)
    request = poller.ingest(ctx, mail("Please quote AAA to BBB, 4 pallets, 600 kg, cheapest.").model_copy(
        update={"id": 9100, "session_id": 9100, "sender": "buyer@hackloop.mail"}))  # fmt: skip
    asked = []

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        asked.append(body)
        reply = request.model_copy(update={"id": 9102, "body": "Sorry, it is 4 pallets of 120 x 80 x 150 cm.",
                                           "in_reply_to": 9101, "references": (9100, 9101)})  # fmt: skip
        poller.ingest(ctx, reply)
        return request.model_copy(update={"id": 9101, "direction": "out"})

    monkeypatch.setattr(outbox, "send", send)
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert "volume" in asked[0] and (rfq.weight_kg, rfq.volume_cbm) == (Decimal(600), Decimal("5.76"))


async def test_an_airline_the_customer_rules_out_stays_out_now_and_later(conn):

    fixtures = Path(__file__).parent / "fixtures"
    scored = json.loads((fixtures / "scored-excluded-airline.json").read_text())
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_carriers(ctx, [{"name": n, "kind": "airline", "email": f"{i}@x", "serves": "AAA"}
                              for i, n in enumerate(scored["airlines"])])  # fmt: skip
    message = to_message(json.loads((fixtures / scored["request"]).read_text()), "in")
    message = message.model_copy(update={"session_id": message.id})
    store.save_zones(
        ctx, [{"zone": zone_of(z), "area": "Town", "airport": "AAA"} for z in extract.POSTCODE.findall(message.body)]
    )
    rfq, _ = await extract.read_request(ctx, message, deadline_for(message.sent_at, settings()))
    assert set(rfq.excluded_carriers) == {scored["excluded"], scored["code"]}
    later = message.model_copy(update={"id": message.id + 1, "session_id": message.id + 1,
                                       "body": message.body.split("\n\nPlease do not")[0]})  # fmt: skip
    rfq, _ = await extract.read_request(ctx, later, deadline_for(later.sent_at, settings()))
    assert scored["excluded"] in rfq.excluded_carriers
    other = later.model_copy(update={"id": later.id + 1, "session_id": later.id + 1, "sender": "someone@else"})
    rfq, _ = await extract.read_request(ctx, other, deadline_for(other.sent_at, settings()))
    assert rfq.excluded_carriers == ()


@pytest.mark.parametrize(
    ("text", "excluded"),
    [
        ("Please avoid Alpha Air on this one.", {"Alpha Air"}),
        ("Bitte nicht mit Alpha Air fliegen.", {"Alpha Air"}),
        ("Any airline except Alpha is fine.", {"Alpha Air"}),
        ("We flew Alpha Air last time and were happy.", set()),
        ("We are not in a hurry.", set()),
    ],
)
def test_excluded_airlines_need_a_negation_and_a_known_name(text, excluded):
    assert set(extract.excluded_airlines(text, {"Alpha Air", "Beta Cargo"})[0]) == excluded


@pytest.mark.parametrize(
    ("text", "ready", "arrive_by", "max_days"),
    [
        (
            "Cargo ready Friday 18 December; must arrive no later than Tuesday 22 December.",
            date(2026, 12, 18),
            date(2026, 12, 22),
            None,
        ),
        (
            "Abholbereit am 28.12.2026. Ankunft spätestens am 4. Januar. Laufzeit höchstens 4 Tage.",
            date(2026, 12, 28),
            date(2027, 1, 4),
            4,
        ),
        ("Ready on December 20, 2026. Maximum transit time: 3 days.", date(2026, 12, 20), None, 3),
        ("- Ready date: 2026-12-29", date(2026, 12, 29), None, None),  # a labelled ready date
        ("The last shipment arrived on 2026-12-01. Transit used to take 3 days.", None, None, None),
        ("Cargo ready 31.11.2026. Ready 02.12.2026 instead.", date(2026, 12, 2), None, None),
        ("Die Sendung muss spätestens am 2026-12-28 am Zielflughafen eintreffen.", None, date(2026, 12, 28), None),
        ("Ihr Angebot muss spätestens am 2026-12-20 bei uns sein.", None, None, None),  # a reply deadline
        ("Die Ware muss spätestens am 2026-12-20 abgeholt werden.", None, None, None),  # a pickup deadline
        ("Die Paletten müssen spätestens bis zum 2026-12-28 in Shanghai ankommen.", None, date(2026, 12, 28), None),
        ("Cargo can be collected from 2026-12-20. Transit time: max 4 days.", date(2026, 12, 20), None, 4),
        ("The goods have to be in Toronto no later than 2026-12-28.", None, date(2026, 12, 28), None),
        ("ETA no later than 2026-12-28, no more than 5 days in transit.", None, date(2026, 12, 28), 5),
        ("Required delivery date: 2026-12-28.", None, date(2026, 12, 28), None),
        ("The shipment will be delivered to FRA by 2026-12-20.", None, None, None),  # our pickup side
        ("The offer has to be in our system by 2026-12-20.", None, None, None),  # a reply deadline
        ("Please quote FRA-ORD 300 kg. The cargo must be delivered to your warehouse at FRA by 2026-12-20.", None, None,
         None),  # the drop-off at our side
        ("The cargo must be delivered at ORD by 2026-12-28.", None, date(2026, 12, 28), None),
        ("Cargo ready date: 2026-12-20, must arrive by 28th December.", date(2026, 12, 20), date(2026, 12, 28), None),
        ("Available for collection as of 20.12.2026.", date(2026, 12, 20), None, None),
        ("Pick-up possible from Dec 20th.", date(2026, 12, 20), None, None),
        ("Earliest collection: 20.12.2026.", date(2026, 12, 20), None, None),
        ("Ready to go on the 20th of December.", date(2026, 12, 20), None, None),
        ("Die Ware ist ab dem 20.12. abholbereit.", date(2026, 12, 20), None, None),
        ("Abholung frühestens am 21.12. möglich.", date(2026, 12, 21), None, None),
        ("We are ready to ship as soon as you confirm.", None, None, None),
    ],
)
def test_schedule_reads_only_stated_readiness_and_limits(text, ready, arrive_by, max_days):
    assert extract.schedule(text, date(2026, 12, 17)) == (ready, arrive_by, max_days)


@pytest.mark.parametrize(
    ("text", "arrive_by"),
    [
        ("Arrival airport is Hong Kong, please reply by Wednesday 25 November.", None),
        ("Please send us your rate with transit and arrival details by 25 November.", None),
        ("Zielflughafen und Ankunft: Hongkong, Angebot bitte bis 25.11.", None),
        ("Please confirm the arrival date by Friday 27 November.", None),
        ("Last time the arrival in HKG was by Monday 16 November, which worked well.", None),
        ("The goods must arrive by 15 June.", None),  # this June, past: no limit; next June's would bind nothing
        ("The goods must arrive in Hong Kong by Wednesday 25 November.", date(2026, 11, 25)),
        ("Please make sure the cargo arrives no later than 2 December.", date(2026, 12, 2)),
        ("Please quote 300 kg FRA-PVG that must arrive by 2 December.", date(2026, 12, 2)),
        ("The arrival date must be no later than 2 December.", date(2026, 12, 2)),
        ("We need the goods to arrive in Shanghai, China by 2 December.", date(2026, 12, 2)),
        ("Wir benötigen ein Angebot für eine Sendung mit Ankunft bis 02.12.", date(2026, 12, 2)),
        ("Arrival airport HKG and rates needed by 25 November.", None),
    ],
)
def test_a_latest_arrival_is_only_one_set_for_this_shipment(text, arrive_by):
    assert extract.schedule(text, date(2026, 11, 22))[1] == arrive_by


def test_a_ready_date_this_autumn_stays_this_year():
    assert extract.schedule("Cargo available from 15 October.", date(2026, 11, 22))[0] == date(2026, 10, 15)


async def test_an_attached_documents_schedule_is_read_and_stands_against_the_mails(conn, monkeypatch):
    """Neither the rules (the mail only) nor `novelty` (sentences a rule matches count as read) read these dates."""

    async def refused(ctx, **kw):
        raise RuntimeError("no model")

    class Box:
        async def attachment(self, message_id, attachment_id):
            return b"SHIPPING INSTRUCTION\nCargo ready on 2026-11-29\nLatest arrival: 2026-12-04\nMax transit 4 days\n"

    monkeypatch.setattr(llm, "ask_json", refused)
    ctx = Context(settings(), conn, Box(), None, None, None)
    message = Message(id=1, session_id=1, direction="in", sender="c@x", to="us@x", subject="RFQ FRA-JFK",
                      body="Please quote FRA to JFK, 300 kg, 1.2 cbm, ready on 2026-11-27, arriving by 2026-12-10. Details attached.",
                      sent_at=RECEIVED, attachments=[Attachment(id=0, filename="si.txt", content_type="text/plain", size=1)])  # fmt: skip
    rfq = await extract.extract(ctx, message, DEADLINE)
    assert (rfq.ready_on, rfq.arrive_by, rfq.max_transit_days) == (date(2026, 11, 29), date(2026, 12, 4), 4)


@pytest.mark.parametrize(
    ("text", "required", "excluded"),
    [
        ("Please quote Alpha Air only, no other airline.", ("Alpha Air",), ()),
        ("Bitte ausschließlich mit Alpha fliegen.", ("Alpha Air",), ()),
        ("Nur Angebote von Alpha Air oder Beta Cargo bitte.", ("Alpha Air", "Beta Cargo"), ()),
        ("Alpha (AA) only, not Beta Cargo.", ("Alpha Air",), ("Beta Cargo",)),
        ("We only ask that you do not use Beta Cargo.", (), ("Beta Cargo",)),
        ("We asked not only Alpha Air but also Beta Cargo.", (), ()),
        ("Only Alpha Air was available last time.", (), ()),
        ("Alpha Air only flies on Fridays.", (), ()),
        ("Only if it is cheaper, use Beta Cargo.", (), ()),
        ("No Beta Cargo, please.", (), ("Beta Cargo",)),
        ("We don't want Beta Cargo for this one.", (), ("Beta Cargo",)),
        ("We have never had issues with Beta Cargo.", (), ()),  # used to rule Beta out
        ("Our contract does not require Beta Cargo.", (), ()),
        ("No pickup needed, Beta Cargo is fine.", (), ()),
        ("Please quote no airline other than Alpha Air.", ("Alpha Air",), ()),
        ("Bitte keine andere Fluggesellschaft außer Alpha Air.", ("Alpha Air",), ()),
        ("Keine andere Airline als Alpha Air oder Beta Cargo.", ("Alpha Air", "Beta Cargo"), ()),
        ("Any airline except Alpha is fine.", (), ("Alpha Air",)),
        ("We can only accept Alpha Air.", ("Alpha Air",), ()),
        ("The shipment must fly with Beta Cargo.", ("Beta Cargo",), ()),
        ("Die Sendung muss mit Alpha fliegen.", ("Alpha Air",), ()),
        ("Alpha Air is our only approved carrier.", ("Alpha Air",), ()),
        ("The shipment doesn't have to fly with Beta Cargo.", (), ()),  # a negation frees the choice
        ("Beta Cargo is the only one we do not use.", (), ("Beta Cargo",)),
        ("Please quote Alpha Air, no other airline.", ("Alpha Air",), ()),
        ("Our frame contract requires Beta Cargo for this lane.", ("Beta Cargo",), ()),
        ("We do not want Beta Cargo, it has to be Alpha Air.", (), ("Beta Cargo",)),  # the negation's own clause
        ("We do not want Beta Cargo, Alpha Air or Gamma Wings.", (), ("Alpha Air", "Beta Cargo")),  # a list goes on
    ],
)
def test_airline_restrictions_apply_to_the_named_carriers(text, required, excluded):
    airlines = {"Alpha Air", "Beta Cargo"}
    assert extract.required_airlines(text, airlines) == required
    assert extract.excluded_airlines(text, airlines)[0] == excluded


@pytest.mark.parametrize(
    "body",
    [
        "Please quote AAA to BBB, 300 kg, 1 cbm. We will arrange the truck to your warehouse ourselves.",
        "Please quote AAA to BBB, 300 kg, 1 cbm, airport to airport. The airline will collect the cargo at BBB.",
        "Please quote AAA to BBB, 300 kg, 1 cbm. Our consignee will collect the goods at the destination airport.",
    ],
)
def test_someone_else_collecting_the_goods_is_no_door_pickup(body):
    assert extract.by_rules(mail(body), DEADLINE).door_pickup is False


LANGUAGE_PROBES = [json.loads(line) for line in (Path(__file__).parent / "fixtures/rfqs/languages.jsonl").open()]


@pytest.mark.parametrize("case", LANGUAGE_PROBES, ids=lambda case: case["case_id"])
def test_the_language_is_read_from_the_customers_own_words(case):
    """Rounds 70 and 72: the body is in any language, the persona's sign-off stays English or German, an older mail
    can be quoted below in another one, and a voice message can carry the whole request."""
    text = extract._text(mail(case["body"], case["subject"]))
    assert extract._language(text, None, case.get("attachment", "")) == case["language"]


@pytest.mark.parametrize(
    "body, attached, language",
    [
        ("Hello,\n\nnew request attached, FRA-JFK.\n\nThanks,\nAnna", "Packliste\nGewicht: 300 kg, die Ware ist nicht gefährlich", "en"),
        ("Hallo,\n\nanbei unsere Anfrage, FRA-JFK.\n\nViele Grüße\nAnna", "Packing list\nThe goods are not dangerous", "de"),
    ],
)  # fmt: skip
def test_a_mail_with_words_of_its_own_is_not_decided_by_its_attachment(body, attached, language):
    assert extract._language(extract._text(mail(body, "Air freight request")), None, attached) == language


async def test_a_question_for_a_missing_fact_is_asked_in_the_customers_language(conn, monkeypatch):
    ctx = Context(settings(), conn, None, None, None, None)
    request = poller.ingest(ctx, mail("Hola, necesitamos una cotización AAA-BBB, 4 cajas, 600 kg. Gracias.").model_copy(
        update={"id": 9200, "session_id": 9200, "sender": "buyer@hackloop.mail"}))  # fmt: skip
    asked = []

    async def fake_ask(ctx, **kw):
        assert kw["purpose"] == "question.translate" and "es" in kw["system"]
        return LlmResult(data={"body": kw["user"].replace("Hello,", "Hola:")}, model="m", tokens_in=1, tokens_out=1,
                         cost_usd=Decimal(0))  # fmt: skip

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        asked.append(body)
        return request.model_copy(update={"id": 9201, "direction": "out"})

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    monkeypatch.setattr(outbox, "send", send)
    monkeypatch.setattr(extract, "ASK_WAIT_S", 0)
    await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert asked[0].startswith("Hola:") and "volume" in asked[0]


@pytest.mark.parametrize(
    "volume, written",
    [("95.00", "a total volume of 95.00 cubic feet"), ("2690", "a volume of 2690 litres")],
)
async def test_a_figure_in_another_unit_is_never_taken_as_kg_or_cbm(conn, monkeypatch, emitted, volume, written):
    """Round 79: "1413.17 lb and a total volume of 95.00 cubic feet". The model copied 95.00 as cbm, the cross-check
    took it, and the quote went out on 15865 kg chargeable instead of 641 kg."""
    ctx = Context(settings(), conn, None, None, None, None)
    said = {"origin": "HAM", "destination": "SGN", "weight_kg": "1413.17", "volume_cbm": "95.00", "pieces": 3,
            "lots": [], "preference": "fastest", "door_pickup": False, "pickup_postcode": None, "currency": "EUR",
            "language": "en", "first_name": None, "intro": None, "closing": None} | {"volume_cbm": volume}  # fmt: skip

    async def model(ctx, **kw):
        return LlmResult(data=said, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", model)
    body = f"HAM to SGN: 3 pieces, a gross weight of 1413.17 lb and {written}. Fastest please."
    rfq = await extract.extract(ctx, mail(body), DEADLINE)
    assert (round(rfq.weight_kg, 2), round(rfq.volume_cbm, 2)) == (Decimal("641.00"), Decimal("2.69"))


async def test_the_cross_check_never_takes_a_net_figure_or_a_destination(conn, monkeypatch, emitted):
    """Review of #250: a net weight beat the packing list's gross, and an airport the customer's answer names in
    passing became the destination."""
    ctx = Context(settings(), conn, None, None, None, None)
    said = {"origin": "AAA", "destination": "CCC", "weight_kg": "400", "volume_cbm": "2.00", "pieces": None,
            "lots": [], "preference": "cheapest", "door_pickup": False, "pickup_postcode": None, "currency": None,
            "language": "en", "first_name": None, "intro": None, "closing": None}  # fmt: skip

    async def model(ctx, **kw):
        return LlmResult(data=said, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    async def attachments(ctx, message):
        return [("packing.pdf", ["Gross weight (kg): 450", "Net weight (kg): 400", "Total volume (cbm): 2.00"])]

    async def asked(*args, **kwargs):
        return "It is 2 cbm. Our sister company ships to CCC as well, but this one goes as stated."

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(extract, "_attachments", attachments)
    packing = Attachment(id=1, filename="packing.pdf", content_type="application/pdf", size=1)
    message = mail("Please quote AAA to BBB, see the packing list attached.").model_copy(
        update={"attachments": (packing,)}
    )
    assert (await extract.extract(ctx, message, DEADLINE)).weight_kg == 450

    monkeypatch.setattr(extract, "_unknown", lambda rfq: ["volume"] if not rfq.volume_cbm else [])
    monkeypatch.setattr(extract, "_ask", asked)
    rfq = await extract.extract(ctx, mail("Please quote AAA to BBB, 500 kg."), DEADLINE)
    assert (rfq.destination, rfq.weight_kg) == ("BBB", 500)


@pytest.mark.parametrize(
    "text, language",
    [
        ("HAM to TLV, 260 kg. Please reply in German, our shipping office does not read English.", "de"),
        ("Bitte FRA nach JFK anbieten, 300 kg. Bitte antworten Sie auf Englisch.", "en"),
        ("We need the cargo in Germany by Friday, please reply asap.", "en"),
    ],
)
def test_a_language_asked_for_wins_over_the_mails_own(text, language):
    assert extract._language(text) == language


@pytest.mark.parametrize(
    "text, postcode",
    [
        (
            "Please collect them from our print shop in 63450 Hanau.\n\nBest regards\nTel. +49 6181 12345\nFax 06181 12346",
            "63450",
        ),
        (
            "Please collect them from our warehouse at Industriestrasse 12, 63450 Hanau, call 06181 12345 first.",
            "63450",
        ),
        (
            "Please collect from our warehouse in 63450 Hanau. Invoice address: Leopoldstrasse 1, 80331 Muenchen.",
            "63450",
        ),
        ("Pickup from our warehouse in 63450 Hanau.\nHead office: Kaiserstrasse 5, 60311 Frankfurt am Main", "63450"),
        ("Please collect from our warehouse in Via Roma 10, 20121 Milano, Italy.", None),
        ("Pickup at I-20121 Milano please.", None),
        ("Pickup from our warehouse at 22041 Hamburg, consignee in 10115 Berlin.", "22041"),
    ],
)
def test_the_pickup_postcode_is_no_phone_number_other_address_or_postcode_abroad(text, postcode):
    # 20121 Milano read as zone 20 asked Hamburg's truckers; a phone number beside the postcode lost it altogether.
    assert extract._pickup(text) == (True, postcode)


async def test_a_departure_airport_the_customer_asks_for_wins_over_the_pickup_zones(conn, monkeypatch, emitted):
    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(
        ctx, [{"zone": "65", "area": "Town", "airport": "AAA"}, {"zone": "80", "area": "City", "airport": "BBB"}]
    )
    body = ("Please quote 480 kg, 1.6 cbm to Hong Kong (HKG), with truck collection from our site in 65189 Town.\n"
            "Please route via City (BBB), not AAA; our letter of credit names BBB.")  # fmt: skip
    assert (await extract.extract(ctx, mail(body), DEADLINE)).origin == "BBB"


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("Please do not route via AAA.", None),  # used to ask for AAA, the airport refused
        ("Please avoid routing via AAA.", None),
        ("Routing via AAA is not possible for us; please use BBB.", "BBB"),
        ("We cannot depart from AAA, so please use BBB.", "BBB"),
        ("Please fly out of BBB.", "BBB"),
        ("Departure from AAA is fine, but BBB would be better.", None),
    ],
)
def test_an_airport_the_customer_refuses_is_never_the_asked_departure(conn, text, asked):
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(
        ctx, [{"zone": "65", "area": "Town", "airport": "AAA"}, {"zone": "80", "area": "City", "airport": "BBB"}]
    )
    assert extract._asked_departure(ctx, text) == asked


@pytest.mark.parametrize(
    "text",
    [
        "We need to ship to Chicago (ORD) 900 kg, 3.5 cbm; please collect from our warehouse in 63450 Hanau.",
        "Commercial invoice attached. Please collect from our warehouse in 63450 Hanau.",
        "Please collect from our warehouse in 63450 Hanau for export to Chicago, USA.",
        "Delivery to SGN airport; pickup from 63450 Hanau.",
        "Ship to JFK. Please collect from our warehouse in 63450 Hanau, Germany.",
        "Please collect from our warehouse in 63450 Hanau, consignee is ACME Inc, 10001 New York, USA.",
    ],
)
def test_a_label_or_country_in_another_clause_keeps_the_pickup_postcode(text):
    # #334 read "invoice", "ship to" or "USA" anywhere before or after the postcode and lost it.
    assert extract._pickup(text) == (True, "63450")


async def test_a_pickup_abroad_asks_nothing_and_the_model_names_the_nearest_of_our_airports(conn, monkeypatch, emitted):
    async def no_question(*args, **kw):
        raise AssertionError("the customer gave the address")

    async def as_the_model_read_it(ctx, **kw):
        assert "AAA, BBB" in kw["user"]
        data = {"origin": "BBB", "destination": "HKG", "weight_kg": "480", "volume_cbm": "1.6", "pieces": 2,
                "preference": "cheapest", "door_pickup": True, "pickup_postcode": "5020", "currency": "EUR",
                "language": "en"}  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(extract, "_ask", no_question)
    monkeypatch.setattr(llm, "ask_json", as_the_model_read_it)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(
        ctx, [{"zone": "65", "area": "Town", "airport": "AAA"}, {"zone": "80", "area": "City", "airport": "BBB"}]
    )
    body = ("Please quote 2 crates to Hong Kong (HKG), 480 kg, 1.6 cbm, collected from our warehouse at "
            "Alpenstrasse 48, 5020 Salzburg, Austria.\n\nBest regards\nAnna")  # fmt: skip
    rfq = await extract.extract(ctx, mail(body), DEADLINE)
    assert (rfq.origin, rfq.door_pickup, rfq.pickup_zone, rfq.missing) == ("BBB", True, None, ())


async def test_a_mail_with_one_line_per_shipment_asks_no_destination(conn, monkeypatch):
    """Round 113: each numbered line names its airport; the model read no one destination and the question went out
    before the quote (rejected one_of_two)."""
    monkeypatch.setattr(llm, "ask_json", lambda ctx, **kw: _model({"origin": "", "destination": "", "weight_kg": "1988",
                        "volume_cbm": "10.07", "pieces": 14, "door_pickup": True, "pickup_postcode": "80602"}))  # fmt: skip
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "80", "area": "Munich", "airport": "CCC"}])

    sent = []

    async def send(ctx, **kw):
        sent.append(kw["purpose"])
        raise outbox.NotSent("held")

    monkeypatch.setattr(outbox, "send", send)
    request = poller.ingest(ctx, captured("rfq-door-two-shipments.json"))
    rfq = await extract.extract(ctx, request, deadline_for(datetime.now(UTC), settings()))
    assert sent == []
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.pickup_zone) == ("CCC", "RUH", Decimal(749), "80")


async def _model(data: dict) -> LlmResult:
    return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))


ROUND_91 = "Please include cargo insurance: goods value 22000 EUR, insured at 110 % of that value, premium rate 0.40 %."


@pytest.mark.parametrize(
    ("text", "premium"),
    [
        (ROUND_91, "96.80"),
        (ROUND_91.replace("110 %", "110 per cent").replace("0.40 %", "0.40 per cent"), "96.80"),
        (ROUND_91.replace("110 %", "110 pct").replace("0.40 %", "0.40 pct"), "96.80"),
        (ROUND_91.replace("0.40 %", "4 ‰"), "96.80"),
        (ROUND_91.replace("insured at 110 % of that value", "insured at the value plus 10 %"), "96.80"),
        (ROUND_91 + " Minimum premium EUR 150.", "150.00"),
        ("Bitte mit Transportversicherung: Warenwert 22.000 EUR, Versicherungswert 110 %, Prämie 4 Promille.", "96.80"),
    ],
)
def test_the_insurance_terms_a_request_states_give_its_premium(text, premium):
    """Round 91 priced the premium on the customer's terms; written another way, the same terms give the same premium."""
    value, _, share, rate, minimum = extract.insurance(text)
    assert calc.insurance_premium(value, share, rate, minimum) == Decimal(premium)


def test_a_past_shipment_beside_the_request_is_not_its_cargo():
    """Round 101 set an earlier offer beside the request; a past shipment's weight is no figure of this one."""
    rfq = extract.by_rules(
        mail("Please quote DUS to RUH, 4 pieces, 990 kg gross, 5.75 cbm. Last month we shipped 1250 kg to RUH."),
        DEADLINE,
    )
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.pieces) == (Decimal(990), Decimal("5.75"), 4)


def test_a_unit_followed_by_a_korean_particle_is_read():
    """Session 39219: "부피는 113.01 cubic feet입니다" ended the unit without a space; the volume was asked for twice."""
    text = "화물은 가구 샘플 12개이며, 총중량은 2028.26 lb입니다, 부피는 113.01 cubic feet입니다."
    assert round(extract._volume(text, 12), 2) == Decimal("3.20")
    assert round(extract._weight(text, 12)) == 920


def test_the_english_article_a_names_no_destination():
    text = "Please send a quote: pickup at our plant in 80331 Munich, a FRA departure is fine, destination ORD, 400 kg."
    assert extract._destination_only(text) == "ORD"


@pytest.mark.parametrize(
    ("pointer", "changed", "origin"),
    [
        ("the same shipment as in my earlier request", "The cargo is unchanged.", "STR"),
        ("the same shipment as in my request", "The cargo is unchanged.", "STR"),
        ("the same shipment as in my request", "The pickup is now from our site in 80331 Munich.", "BBB"),
        ("the same shipment again. My earlier request was", "The cargo is unchanged.", "STR"),  # round 114
    ],
)
async def test_the_same_shipment_as_an_earlier_request_is_read_from_it(conn, monkeypatch, pointer, changed, origin):
    """Rounds 103 and 104 pointed to the customer's earlier request by its subject and named only a new destination;
    the scorer priced the earlier cargo (103 accepted, 104 lost when the model read no weight). The quoted subject's
    lane is the earlier one, not this request's."""

    async def boom(ctx, **kw):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "80", "area": "City", "airport": "BBB"}])
    earlier = extract.by_rules(mail("Please quote STR to MEL, 6 pieces, 1841 kg gross, 4.63 cbm, cheapest."), DEADLINE)
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (1, 'buyer@example.com', 'quoted', ?)",
                 (RECEIVED.isoformat(),))  # fmt: skip
    conn.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (1, 1, 'in', 'buyer@example.com', 'us@pixel', 'Air freight quote request: STR to MEL', '', ?, '{}')",
        (RECEIVED.isoformat(),),
    )
    conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, 1, 'rfq.extracted', 'rfq', ?)",
                 (RECEIVED.isoformat(), earlier.model_dump_json()))  # fmt: skip
    request = mail(
        f'Please quote {pointer}, "Air freight quote request: STR to MEL". {changed} The new destination is LAX. '
        "Please send the cheapest all-in price in EUR."
    ).model_copy(update={"id": 2, "session_id": 2})
    rfq = await extract.extract(ctx, request, DEADLINE)
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm, rfq.pieces) == (
        origin, "LAX", Decimal(1841), Decimal("4.63"), 6
    )  # fmt: skip


@pytest.mark.parametrize(
    "changed",
    [
        "The cargo is unchanged, but please ship it from FRA instead of STR.",
        "The cargo is unchanged. Origin is now FRA.",
        "Same cargo, but it should now go via FRA.",
    ],
)
async def test_a_new_origin_of_the_same_shipment_never_becomes_its_destination(conn, monkeypatch, changed):
    async def boom(ctx, **kw):
        raise AssertionError("model")

    monkeypatch.setattr(llm, "ask_json", boom)
    ctx = Context(settings(), conn, None, None, None, None)
    store.save_zones(ctx, [{"zone": "80", "area": "City", "airport": "BBB"}])
    earlier = extract.by_rules(mail("Please quote STR to MEL, 6 pieces, 1841 kg gross, 4.63 cbm, cheapest."), DEADLINE)
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (1, 'buyer@example.com', 'quoted', ?)",
                 (RECEIVED.isoformat(),))  # fmt: skip
    conn.execute("INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data) VALUES"
                 " (1, 1, 'in', 'buyer@example.com', 'us@pixel', 'Air freight quote request: STR to MEL', '', ?, '{}')",
                 (RECEIVED.isoformat(),))  # fmt: skip
    conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, 1, 'rfq.extracted', 'rfq', ?)",
                 (RECEIVED.isoformat(), earlier.model_dump_json()))  # fmt: skip
    body = (f'Please quote the same shipment as in my earlier request, "Air freight quote request: STR to MEL". '
            f"{changed} Please send the cheapest all-in price in EUR.")  # fmt: skip
    request = mail(body).model_copy(update={"id": 2, "session_id": 2})
    try:
        rfq = await extract.extract(ctx, request, DEADLINE)
    except AssertionError as e:  # the rules leave it to the model, as before
        assert "model" in str(e)
        return
    assert rfq.destination != "FRA"


async def test_a_photographed_request_goes_to_the_model_as_its_page(conn, monkeypatch):
    """No code reads a photo as text: its page goes with the one call the request costs, and the model's facts count
    when the mail states none of its own."""
    import io

    from PIL import Image

    with io.BytesIO() as stream:
        Image.new("RGB", (40, 20), "white").save(stream, format="PNG")
        png = stream.getvalue()

    class Files:
        async def attachment(self, message_id, attachment_id):
            return png

    seen = []

    async def model(ctx, *, purpose, images=(), **kw):
        seen.append((purpose, images))
        data = {k: None for k in extract.SCHEMA["required"]} | {
            "origin": "AAA", "destination": "BBB", "weight_kg": "480", "volume_cbm": "2.10", "pieces": 3, "lots": [],
            "preference": "cheapest", "door_pickup": False, "language": "en",
        }  # fmt: skip
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", model)
    ctx = Context(settings(), conn, Files(), None, None, None)
    photo = Attachment(id=1, filename="request.jpg", content_type="image/jpeg", size=10)
    request = mail("Hello, our request is in the attached photo.").model_copy(update={"attachments": (photo,)})
    rfq = await extract.extract(ctx, request, DEADLINE)
    assert [(p, len(i), i[0][:2]) for p, i in seen if p == "rfq.extract"] == [("rfq.extract", 1, b"\xff\xd8")]
    assert (rfq.origin, rfq.destination, rfq.weight_kg, rfq.volume_cbm) == ("AAA", "BBB", Decimal(480), Decimal("2.10"))


async def test_a_cargo_photo_beside_a_written_request_never_rides_along(conn, monkeypatch):
    """The mail names its lane and weight: a photo of the crates is not sent with the model's reading."""
    import io

    from PIL import Image

    with io.BytesIO() as stream:
        Image.new("RGB", (40, 20), "white").save(stream, format="PNG")
        png = stream.getvalue()

    class Files:
        async def attachment(self, message_id, attachment_id):
            return png

    seen = []

    async def model(ctx, *, purpose, images=(), **kw):
        seen.append(images)
        raise RuntimeError("no model")

    monkeypatch.setattr(llm, "ask_json", model)
    ctx = Context(settings(), conn, Files(), None, None, None)
    photo = Attachment(id=1, filename="cargo.jpg", content_type="image/jpeg", size=10)
    body = "Please quote FRA to JFK, 480 kg, crates of 120x80x100 cm and 100x60x80 cm, see the photo."  # model reads it
    request = mail(body).model_copy(update={"attachments": (photo,)})
    try:
        await extract.extract(ctx, request, DEADLINE)
    except RuntimeError:
        pass
    assert seen and all(images == () for images in seen)
