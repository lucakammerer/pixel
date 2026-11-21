from test_crm import mail, quote

from pixel.agents.tracking import agent
from pixel.crm import shipments

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
    assert s.id in sent["body"] and "confirmed by Kestrel Airways" in sent["body"] and "FRA → JFK" in sent["body"]


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
