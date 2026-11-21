import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from pixel import llm
from pixel.agents.rfq import agent, check, compose, extract
from pixel.contracts import LlmResult, RateOption, Source
from pixel.core import events, feedback, poller, timing
from pixel.core.orchestrator import deadline_for
from pixel.crm import quotes
from pixel.pricing import calc, engine, session
from tests.test_outbox import make_ctx
from tests.test_rfq_airports import mail


@pytest.fixture
def request_flow(conn, monkeypatch):
    monkeypatch.delenv("PIXEL_SELF_HEAL", raising=False)
    monkeypatch.delenv("PIXEL_ASK_MISSING", raising=False)
    ctx = make_ctx(conn, precheck="on")
    message = mail("AAA to BBB. 4 cartons, each 30 kg. Volume: 0.1 cbm.").model_copy(update={"sent_at": timing.now()})
    poller.ingest(ctx, message)
    deadline = deadline_for(message.sent_at, ctx.settings)
    priced = []

    async def options(ctx, rfq, deadline):
        priced.append(rfq)
        weight = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
        total = weight * 2
        result = RateOption(
            source=Source.SHEET, carrier="Test airline", origin=rfq.origin, destination=rfq.destination,
            chargeable_kg=weight, billed_kg=weight, freight=total, total=total, currency="EUR", total_eur=total,
            transit_days=1,
        )  # fmt: skip
        engine._found(ctx, rfq, result)
        # Simulate the engine's carrying forward any offers it found on the lane before repricing.
        return session.for_request(ctx, rfq)

    async def prose(*args):
        return None

    monkeypatch.setattr(engine, "options", options)
    monkeypatch.setattr(compose, "prose", prose)
    return ctx, message, deadline, priced


def rules_read(monkeypatch, weight):
    async def read_request(ctx, message, deadline):
        rfq = extract.by_rules(message, deadline).model_copy(update={"weight_kg": Decimal(weight)})
        events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
        return rfq, extract._text(message)

    monkeypatch.setattr(extract, "read_request", read_request)


def heal_model(monkeypatch, mode="corrects"):
    calls = []

    async def ask(ctx, **kwargs):
        calls.append(kwargs)
        if mode == "error":
            raise RuntimeError("unavailable")
        if mode == "timeout":
            await asyncio.sleep(60)
        data = {"origin": "AAA", "destination": "BBB", "weight_kg": "120", "volume_cbm": "0.1", "pieces": 4}
        return LlmResult(data=data, model="test", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", ask)
    return calls


async def test_weight_block_reprices_once_and_sends_the_repaired_quote(request_flow, monkeypatch):
    ctx, message, deadline, priced = request_flow
    rules_read(monkeypatch, 30)
    calls = heal_model(monkeypatch)
    quote = await agent.handle(ctx, message, deadline)
    assert quote.rfq.weight_kg == 120 and quote.total == 240 and quote.rfq.extracted_by == "model"
    assert [rfq.weight_kg for rfq in priced] == [30, 120]
    assert len(calls) == 1 and calls[0] == {
        "purpose": "rfq.heal", "system": extract.SYSTEM, "schema": extract.SCHEMA, "user": extract._text(message),
        "session_id": message.session_id, "max_output_tokens": 500,
    }  # fmt: skip
    log = events.for_session(ctx, message.session_id)
    assert sum(event.kind == "quote.heal_attempted" for event in log) == 1
    (healed,) = (event.payload for event in log if event.kind == "quote.healed")
    assert "weight_unexplained" in healed["before"]["blocking"]
    assert healed["after"] == {"quote": quote.id, "blocking": [], "total": "240"}
    assert quotes.for_session(ctx, message.session_id) == quote
    assert len(ctx.mailbox.sent) == 1
    assert "quote_doubtful" not in {issue["issue"] for issue in feedback.attention(ctx.db, 5)}


@pytest.mark.parametrize("skip", ["no_block", "near_target", "disabled", "precheck_off", "already_attempted"])
async def test_heal_skips_without_a_qualifying_first_attempt(request_flow, monkeypatch, skip):
    ctx, message, deadline, priced = request_flow
    rules_read(monkeypatch, 120 if skip == "no_block" else 30)
    if skip == "near_target":
        deadline = replace(deadline, target_at=timing.now() + timedelta(seconds=14))
    elif skip == "disabled":
        monkeypatch.setenv("PIXEL_SELF_HEAL", "off")
    elif skip == "precheck_off":
        ctx.settings = replace(ctx.settings, precheck="off")
    elif skip == "already_attempted":
        events.emit(ctx, "quote.heal_attempted", "rfq", session_id=message.session_id)
    calls = heal_model(monkeypatch)
    quote = await agent.handle(ctx, message, deadline)
    assert quote.rfq.weight_kg == priced[0].weight_kg
    assert len(priced) == 1 and calls == []
    assert len(ctx.mailbox.sent) == 1


@pytest.mark.parametrize("mode", ["error", "timeout", "still_blocks"])
async def test_failed_heal_sends_original_and_leaves_it_available_to_fallback(request_flow, monkeypatch, mode):
    ctx, message, deadline, priced = request_flow
    rules_read(monkeypatch, 30)
    if mode == "timeout":
        monkeypatch.setattr(agent, "HEAL_MIN_S", 0)
        monkeypatch.setattr(agent, "HEAL_SEND_RESERVE_S", 0)
        now = timing.now()
        monkeypatch.setattr(timing, "now", lambda: now)
        deadline = replace(deadline, target_at=now + timedelta(seconds=0.2))
    if mode == "still_blocks":
        run = check.run

        def check_candidate(quote, *args):
            findings = run(quote, *args)
            if quote.rfq.extracted_by == "model":
                findings.append(check.Finding("total", check.BLOCK, "unusable candidate total"))
            return findings

        monkeypatch.setattr(check, "run", check_candidate)
    calls = heal_model(monkeypatch, mode)
    quote = await agent.handle(ctx, message, deadline)
    assert len(calls) == 1
    assert len(priced) == (2 if mode == "still_blocks" else 1)
    assert quote.rfq.weight_kg == 30 and quote.total == 60
    log = events.for_session(ctx, message.session_id)
    assert not any(event.kind == "quote.healed" for event in log)
    assert (await agent.fallback(ctx, message.session_id)).total == 60


@pytest.mark.parametrize("disable", ["env", "feedback"])
async def test_disabled_question_still_allows_the_live_quote(request_flow, monkeypatch, disable):
    ctx, message, deadline, priced = request_flow
    message = message.model_copy(update={"body": "AAA to BBB. Gross weight: 120 kg."})
    if disable == "env":
        monkeypatch.setenv("PIXEL_ASK_MISSING", "0")
    else:
        events.emit(ctx, "feedback", "board", session_id=99, payload={"question_counted_as_quote": True})
    quote = await agent.handle(ctx, message, deadline)
    assert quote.sent_at is not None and len(priced) == 1
    deliveries = ctx.db.execute("SELECT purpose FROM messages WHERE direction='out'").fetchall()
    assert [row["purpose"] for row in deliveries] == ["quote"]


def test_heal_preserves_confirmed_pickup_and_document_evidence(request_flow):
    ctx, message, deadline, _ = request_flow
    original = extract.by_rules(message, deadline).model_copy(
        update={"door_pickup": True, "pickup_zone": "42", "pickup_postcode": "42000",
                "missing": (extract.DG_DOCUMENTS,)}
    )  # fmt: skip
    events.emit(
        ctx, "rfq.evidence", "rfq", session_id=message.session_id, message_id=message.id,
        payload={"sources": {"weight_kg": ["packing.pdf"]}},
    )  # fmt: skip
    model = original.model_copy(
        update={"origin": "BBB", "weight_kg": Decimal(30), "volume_cbm": Decimal(2),
                "door_pickup": False, "pickup_zone": None, "pickup_postcode": None, "missing": ()}
    )  # fmt: skip
    healed = agent._heal_request(ctx, original, model, "To BBB. Collect at our warehouse.")
    assert (healed.origin, healed.pickup_zone, healed.pickup_postcode) == ("AAA", "42", "42000")
    assert healed.door_pickup and healed.missing == (extract.DG_DOCUMENTS,)
    assert (healed.weight_kg, healed.volume_cbm) == (original.weight_kg, Decimal(2))
