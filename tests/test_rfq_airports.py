import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import agent
from pixel.config import settings
from pixel.contracts import LlmResult, Message, Rfq
from pixel.core.context import Context
from pixel.pricing import store

RECEIVED = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


def mail(body: str) -> Message:
    return Message(id=7, session_id=7, direction="in", sender="buyer@example.com", to="us@example.com",
                   subject="Air freight quote request", body=body, sent_at=RECEIVED)  # fmt: skip


def request(origin: str, destination: str) -> Rfq:
    return Rfq(session_id=7, message_id=7, customer="buyer@example.com", origin=origin, destination=destination,
               weight_kg=Decimal(855), received_at=RECEIVED, deadline_at=RECEIVED)  # fmt: skip


def served(conn) -> Context:
    ctx = Context(settings(), conn, None, None, None, None)  # no portal: the store alone tells
    store.save_zones(ctx, [{"zone": "11", "area": "Town", "airport": "AAA"}])
    store.save_carriers(ctx, [{"name": "Air", "kind": "airline", "email": "air@example.com", "serves": "CCC DDD"}])
    return ctx


def answers(monkeypatch, data: dict) -> list[dict]:
    asked = []

    async def ask_json(ctx, **kw):
        asked.append(kw)
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", ask_json)
    return asked


async def test_a_guessed_origin_no_lane_starts_at_is_corrected_to_a_served_airport(conn, monkeypatch):
    asked = answers(monkeypatch, {"origin": "AAA", "destination": "CCC"})
    rfq = await agent.served_airports(served(conn), request("XXX", "CCC"), mail("Pickup in Town, to CCC, 855 kg."))
    assert (rfq.origin, rfq.destination) == ("AAA", "CCC")
    assert len(asked) == 1 and "AAA" in asked[0]["user"]


async def test_served_codes_take_no_model_call(conn, monkeypatch):
    asked = answers(monkeypatch, {})
    rfq = await agent.served_airports(served(conn), request("AAA", "DDD"), mail("Town to DDD, 855 kg."))
    assert (rfq.origin, rfq.destination, asked) == ("AAA", "DDD", [])


async def test_a_code_the_mail_names_stands(conn, monkeypatch):
    asked = answers(monkeypatch, {})
    rfq = await agent.served_airports(served(conn), request("XXX", "CCC"), mail("From XXX to CCC, 855 kg."))
    assert (rfq.origin, asked) == ("XXX", [])


async def test_an_answer_outside_the_served_airports_is_not_taken(conn, monkeypatch):
    answers(monkeypatch, {"origin": "YYY", "destination": "CCC"})
    rfq = await agent.served_airports(served(conn), request("XXX", "CCC"), mail("Pickup in Town, to CCC."))
    assert rfq.origin == "XXX"


async def test_a_hanging_airport_check_never_holds_the_pricing(conn, monkeypatch):
    async def hangs(ctx, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(llm, "ask_json", hangs)
    monkeypatch.setattr(agent, "AIRPORTS_WAIT_S", 0.1)
    rfq = await agent.served_airports(served(conn), request("XXX", "CCC"), mail("Pickup in Town, to CCC, 855 kg."))
    assert rfq.origin == "XXX"  # priced as read, at once
