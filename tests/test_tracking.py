import json
from decimal import Decimal
from pathlib import Path

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


def shipped(ctx, session_id, customer, origin, destination, kg, postcode=None):
    q = quote(session_id, customer=customer)
    rfq = q.rfq.model_copy(update={"origin": origin, "destination": destination, "weight_kg": Decimal(kg),
                                   "door_pickup": bool(postcode), "pickup_postcode": postcode})  # fmt: skip
    q = q.model_copy(
        update={"rfq": rfq, "option": q.option.model_copy(update={"origin": origin, "destination": destination})}
    )
    quotes.save(ctx, q)
    s = shipments.create(ctx, q).model_copy(update={"booking_reference": f"BK-{session_id:08d}", "status": "confirmed"})
    shipments.save(ctx, s)
    return s


async def test_the_desk_describes_a_door_shipment_by_pickup_postcode_weight_and_company(pipe):
    ours = shipped(pipe.ctx, 1, "lindqvist-medtech@hackloop.mail", "CGN", "ORD", 676, postcode="51447")
    same_destination = shipped(pipe.ctx, 2, "someone-else@hackloop.mail", "FRA", "ORD", 676)
    await agent.handle(pipe.ctx, to_message(json.loads((MAILS / "tracking-door-to-port.json").read_text()), "in"))
    body = pipe.sent[0]["body"]
    assert ours.id in body and ours.booking_reference in body and same_destination.id not in body
    # The desk signs as the customer and gives the ready date: answer them by name, with the arrival, in sentences.
    assert body.startswith("Hi Sara,") and "Sunday 29 November" in body and "@" not in body


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
