import json
from decimal import Decimal
from pathlib import Path

import pytest
from test_crm import mail, quote

from pixel.agents.tracking import agent
from pixel.api.mailbox import to_message
from pixel.crm import quotes, shipments

MAILS = Path(__file__).parent / "fixtures" / "mails"

pytest_plugins = ["test_crm"]  # the `pipe` fixture


def booked(ctx):
    s = shipments.create(ctx, quote(1))
    s = s.model_copy(update={"booking_reference": "BK-AB12CD34", "status": "confirmed"})
    shipments.save(ctx, s)
    return s


async def test_answers_from_the_crm_by_booking_reference(pipe):
    s = booked(pipe.ctx)
    await agent.handle(pipe.ctx, mail(40, 40, sender="tracking@hackloop.mail", subject="Status?",
                                      body="Where is booking BK-AB12CD34?"))  # fmt: skip
    (sent,) = pipe.sent
    assert sent["to"] == "tracking@hackloop.mail" and sent["in_reply_to"] == 40
    assert s.id in sent["body"] and "Kestrel Airways" in sent["body"] and "confirmed" in sent["body"]
    assert "BK-AB12CD34" in sent["body"] and "from FRA to JFK" in sent["body"]


async def test_finds_by_customer_and_lane(pipe):
    s = booked(pipe.ctx)
    await agent.handle(pipe.ctx, mail(41, 41, sender="tracking@hackloop.mail",
                                      body="Our customer buyer@acme.example asks about FRA to JFK."))  # fmt: skip
    assert s.id in pipe.sent[0]["body"]


async def test_unknown_shipment_gets_an_honest_answer(pipe):
    booked(pipe.ctx)
    await agent.handle(pipe.ctx, mail(42, 42, sender="tracking@hackloop.mail", body="Where is BK-ZZZZ9999?"))
    body = pipe.sent[0]["body"]
    assert "no shipment under BK-ZZZZ9999" in body and "confirmed" not in body


def shipped(ctx, session_id, customer, origin, destination, kg, postcode=None, zone=None):
    q = quote(session_id, customer=customer)
    rfq = q.rfq.model_copy(update={"origin": origin, "destination": destination, "weight_kg": Decimal(kg),
                                   "door_pickup": bool(postcode or zone), "pickup_postcode": postcode,
                                   "pickup_zone": zone or (postcode[:2] if postcode else None)})  # fmt: skip
    q = q.model_copy(
        update={"rfq": rfq, "option": q.option.model_copy(update={"origin": origin, "destination": destination})}
    )
    quotes.save(ctx, q)
    s = shipments.create(ctx, q).model_copy(update={"booking_reference": f"BK-{session_id:08d}", "status": "confirmed"})
    shipments.save(ctx, s)
    return s


def test_described_shipment_matches_rounded_weight(pipe):
    ours = shipped(pipe.ctx, 1, "buyer@acme.example", "CGN", "ORD", "676.4")
    shipped(pipe.ctx, 2, "buyer@acme.example", "CGN", "ORD", 900)
    assert agent.described(pipe.ctx, "CGN to ORD, 676 kg") == [ours]


def test_described_shipment_rejects_wrong_weight(pipe):
    shipped(pipe.ctx, 1, "buyer@acme.example", "CGN", "ORD", 676)
    found, _ = agent.match(pipe.ctx, "CGN to ORD, 999 kg", "tracking@hackloop.mail")
    assert found == []


def test_described_shipment_rejects_wrong_postcode(pipe):
    shipped(pipe.ctx, 1, "buyer@acme.example", "CGN", "ORD", 676, postcode="51447")
    found, _ = agent.match(pipe.ctx, "61018 to ORD, 676 kg for buyer@acme.example", "tracking@hackloop.mail")
    assert found == []


async def test_the_desk_describes_a_door_shipment_by_pickup_postcode_weight_and_company(pipe):
    ours = shipped(pipe.ctx, 1, "lindqvist-medtech@hackloop.mail", "CGN", "ORD", 676, postcode="51447")
    same_destination = shipped(pipe.ctx, 2, "someone-else@hackloop.mail", "FRA", "ORD", 676)
    await agent.handle(pipe.ctx, to_message(json.loads((MAILS / "tracking-door-to-port.json").read_text()), "in"))
    body = pipe.sent[0]["body"]
    assert ours.id in body and ours.booking_reference in body and same_destination.id not in body
    # The desk signs as the customer and gives the ready date: answer them by name, with the arrival, in sentences.
    assert body.startswith("Hi Sara,") and "Sunday 29 November" in body
    assert "lindqvist-medtech@hackloop.mail" not in body and "51447" not in body


@pytest.mark.parametrize("variant", ["scored", "a", "b", "c"])
def test_every_variant_carries_every_fact_the_scored_answer_carried(pipe, variant):
    s = shipped(pipe.ctx, 1, "lindqvist-medtech@hackloop.mail", "CGN", "ORD", 676, postcode="51447")
    body = agent.answer([s], None, details={s.id: agent.Detail(postcode="51447", town="Bergisch Gladbach")},
                        variant=variant)  # fmt: skip
    for fact in (s.id, s.booking_reference, "Kestrel Airways", "booked and ready to ship", "3 days", "CGN", "ORD",
                 "676 kg"):  # fmt: skip
        assert fact in body
    assert ("lindqvist-medtech@hackloop.mail" in body) == (variant == "scored")


async def test_the_desk_describes_a_port_shipment_by_lane_weight_and_company(pipe):
    ours = shipped(pipe.ctx, 1, "auer-maschinenbau@hackloop.mail", "DUS", "JFK", 1540)
    lighter = shipped(pipe.ctx, 2, "auer-maschinenbau@hackloop.mail", "DUS", "JFK", 300)
    await agent.handle(pipe.ctx, mail(43, 43, sender="tracking@hackloop.mail", subject="Status of our shipment DUS to JFK, 1540 kg",
                                      body="Auer Maschinenbau GmbH | DUS to JFK | 1540 kg. Where is it now?"))  # fmt: skip
    body = pipe.sent[0]["body"]
    assert ours.id in body and lighter.id not in body


async def test_a_described_shipment_we_do_not_hold_is_not_guessed(pipe):
    shipped(pipe.ctx, 1, "lindqvist-medtech@hackloop.mail", "CGN", "ORD", 676, postcode="51447")
    await agent.handle(pipe.ctx, mail(44, 44, sender="tracking@hackloop.mail",
                                      body="Nordwind GmbH | 20095 to ORD | 410 kg. Where is it now?"))  # fmt: skip
    assert "no shipment" in pipe.sent[0]["body"]


def test_exact_weight_and_exact_pickup_win_and_ambiguous_descriptions_disclose_no_records(pipe):
    ctx = pipe.ctx
    a = shipped(ctx, 1, "buyer@example.invalid", "AAA", "BBB", 676, postcode="51447")
    b = shipped(ctx, 2, "buyer@example.invalid", "AAA", "BBB", 680, postcode="51447")
    assert agent.described(ctx, "AAA to BBB, 676 kg") == [a]
    assert agent.described(ctx, "AAA to BBB, 678 kg") == []
    shipped(ctx, 3, "buyer@example.invalid", "AAA", "BBB", 676)
    assert agent.described(ctx, "51447 to BBB, 676 kg") == [a]
    assert agent.described(ctx, "AAA to BBB, 676 kg") == []
    assert agent.match(ctx, "Other ref BK-UNKNOWN, AAA to BBB, 680 kg", "desk@example.invalid")[0] == []
    assert b not in agent.described(ctx, "AAA to BBB, 680 kg for stranger@example.invalid")


def test_a_remembered_site_or_a_replaced_origin_still_finds_the_shipment(pipe):
    """Our pickup postcode came from the customer's earlier site (same zone), or the zone's airport replaced the
    origin the desk names: the desk's description still names one shipment of that company and weight."""
    site = shipped(pipe.ctx, 1, "lindqvist-medtech@hackloop.mail", "CGN", "ORD", 676, postcode="51465")
    assert agent.described(pipe.ctx, "Lindqvist Medtech GmbH | 51447 to ORD | 676 kg") == [site]
    zone = shipped(pipe.ctx, 2, "auer-maschinenbau@hackloop.mail", "DUS", "PEK", 1081)
    assert agent.described(pipe.ctx, "Auer Maschinenbau GmbH, CGN to PEK, 1081 kg") == [zone]


async def test_the_answer_names_the_town_of_the_pickup_the_desk_asked_about(pipe):
    """Round 19: "our warehouse" was filled in as the customer's earlier site 51896; the desk asked for 51804. An
    answer "from 51896" would contradict the question; a clerk names the town, not a postcode."""
    ours = shipped(pipe.ctx, 1, "rheintal-elektronik@hackloop.mail", "CGN", "DFW", 1795, postcode="51896")
    pipe.ctx.db.execute("INSERT INTO pickup_zones VALUES ('51', 'Bergisch Gladbach', 'CGN')")
    await agent.handle(pipe.ctx, mail(45, 45, sender="tracking@hackloop.mail",
                                      body="Rheintal Elektronik GmbH, 51804 to DFW, 1795 kg, ready on 2026-11-29?"))  # fmt: skip
    body = pipe.sent[0]["body"]
    assert ours.booking_reference in body and "from your site in Bergisch Gladbach, flying CGN to DFW" in body
    assert "51896" not in body


@pytest.mark.parametrize(
    ("closing", "bye"),
    [
        ("Viele Gruesse / Many greetings\nDeniz Yilmaz", "Viele Grüße\nLena Berger"),
        ("Best greetings from Regensburg\nTobias Auer", "Best regards to Regensburg\nLena Berger"),
        ("Sara\nLindqvist Medtech", "Best regards\nLena Berger"),
    ],
)
def test_the_tracking_answer_signs_off_as_the_quote_did(closing, bye):
    body = agent.answer([], None, f"Hello,\n\nwhere is it now?\n\n{closing}")
    assert body.endswith(f"\n\n{bye}\nAir Export, PIXEL Air Freight")


def test_a_company_inside_a_compound_word_is_recognised_and_never_answered_with_another_customer(pipe):
    ours = shipped(pipe.ctx, 1, "sonnberg-roesterei@hackloop.mail", "FRA", "JFK", 1233)  # question 921
    assert agent.described(pipe.ctx, "Sonnberg Kaffeeroesterei GmbH, HHN to JFK, 1233 kg") == [ours]
    shipped(pipe.ctx, 2, "rheintal-elektronik@hackloop.mail", "CGN", "ORD", 679, postcode="51896")
    pipe.ctx.db.execute("INSERT INTO sessions (id, customer, state, received_at, deadline_at) VALUES "
                        "(3, 'lindqvist-medtech@hackloop.mail', 'open', '', '')")  # fmt: skip
    assert agent.described(pipe.ctx, "Lindqvist Medtech GmbH | 51447 to ORD | 676 kg") == []
