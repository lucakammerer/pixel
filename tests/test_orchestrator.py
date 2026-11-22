import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pixel.agents.rfq import agent as rfq_agent
from pixel.core import events, orchestrator, outbox, poller, quote_recovery
from pixel.core.context import Context
from pixel.crm import quotes
from pixel.pricing import portal_rates, store
from tests.test_extract import mail
from tests.test_outbox import ingest_rfq, kinds, make_ctx, make_quote


def fake_clock(monkeypatch, rfq, ctx, before_hard_s=0.05):
    hard_at = orchestrator.deadline_for(rfq.sent_at, ctx.settings).hard_at
    monkeypatch.setattr(orchestrator, "now", lambda: hard_at - timedelta(seconds=before_hard_s))


async def test_recovery_takes_over_at_two_minutes_thirty_from_receipt(conn, monkeypatch):
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx)
    clock = message.sent_at + timedelta(seconds=150)
    monkeypatch.setattr(orchestrator, "now", lambda: clock)
    monkeypatch.setattr(quote_recovery.timing, "now", lambda: clock)
    monkeypatch.setattr(orchestrator, "FALLBACK_POLL_S", 0.01)
    quote = make_quote(message)
    primary_stopped = asyncio.Event()

    async def handle(*args):
        events.emit(ctx, "rfq.extracted", "rfq", session_id=message.id, payload=quote.rfq.model_dump(mode="json"))
        try:
            await asyncio.Future()
        finally:
            primary_stopped.set()

    async def portal(ctx, rfq, deadline):
        assert primary_stopped.is_set()
        assert deadline.target_at <= deadline.hard_at
        events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                    payload={"option": quote.option.model_dump(mode="json")})  # fmt: skip

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(quote_recovery.portal_rates, "options", portal)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, message), timeout=1)
    history = events.for_session(ctx, message.id)
    assert [ev.payload["reason"] for ev in history if ev.kind == "quote.recovery"] == ["timeout"]
    assert ctx.mailbox.sent == [message.sender]
    assert kinds(ctx)[-2:] == ["quote.sent", "quote.fallback"]


async def test_stalled_recovery_cannot_delay_the_hard_deadline(conn, monkeypatch):
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx)
    fake_clock(monkeypatch, message, ctx)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def recovery(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def handle(*args):
        return None

    async def fallback(ctx, session_id, *, allow_partial=False):
        return make_quote(message) if allow_partial else None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    monkeypatch.setattr(quote_recovery, "source", recovery)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, message), timeout=1)
    assert started.is_set() and stopped.is_set()
    assert ctx.mailbox.sent == [message.sender]


async def test_fallback_sends_at_hard_at_when_the_agent_hangs(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx)
    hung = asyncio.Event()

    async def handle(ctx, message, deadline):
        await hung.wait()

    async def fallback(ctx, session_id, *, allow_partial=False):
        return make_quote(rfq, "Q-fallback")

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, rfq), timeout=1)
    assert len(ctx.mailbox.sent) == 1
    assert kinds(ctx)[-2:] == ["quote.sent", "quote.fallback"]


async def test_missed_when_there_is_no_price(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx)

    async def handle(ctx, message, deadline):
        raise RuntimeError("no lane")

    async def fallback(ctx, session_id, *, allow_partial=False):
        return None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await orchestrator.quote_with_deadline(ctx, rfq)
    assert len(ctx.mailbox.sent) == 1
    await orchestrator.quote_with_deadline(ctx, rfq)
    assert ctx.mailbox.sent == [rfq.sender]
    reply = conn.execute("SELECT in_reply_to, body FROM messages WHERE purpose='no_price'").fetchone()
    assert reply["in_reply_to"] == rfq.id
    assert "could not obtain a freight rate" in reply["body"]
    assert "quote.missed" in kinds(ctx)


async def test_an_agent_that_gives_up_early_still_quotes_a_price_that_arrives_before_hard_at(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx, before_hard_s=1.0)
    monkeypatch.setattr(orchestrator, "FALLBACK_POLL_S", 0.01)
    calls = []

    async def handle(ctx, message, deadline):
        return None  # no price yet: a late bid is still on its way

    async def fallback(ctx, session_id, *, allow_partial=False):
        calls.append(session_id)
        return make_quote(rfq, "Q-late") if len(calls) >= 3 else None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, rfq), timeout=1)
    assert len(ctx.mailbox.sent) == 1
    assert "quote.missed" not in kinds(ctx)
    assert kinds(ctx).count("quote.waiting") == 1


async def test_fallback_waits_for_the_requested_airline_and_labels_an_alternative(conn):
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx)
    quote = make_quote(message)
    requested_carrier = "Requested Air"
    rfq = quote.rfq.model_copy(update={"required_carriers": (requested_carrier,)})
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": quote.option.model_dump(mode="json")})  # fmt: skip

    assert await rfq_agent.fallback(ctx, rfq.session_id) is None
    alternative = await rfq_agent.fallback(ctx, rfq.session_id, allow_partial=True)
    assert alternative.option.carrier == quote.option.carrier
    assert "The price above is for the alternative shown." in alternative.body

    requested = quote.option.model_copy(update={"carrier": requested_carrier, "transit_days": 10})
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": requested.model_dump(mode="json")})  # fmt: skip
    compliant = await rfq_agent.fallback(ctx, rfq.session_id)
    assert compliant.option.carrier == requested_carrier
    assert "alternative shown" not in compliant.body


async def test_a_missed_arrival_date_is_quoted_at_once_with_the_alternative_named(conn):
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx)
    quote = make_quote(message)
    rfq = quote.rfq.model_copy(update={"arrive_by": quote.rfq.received_at.date()})  # no option arrives that soon
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
    events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                payload={"option": quote.option.model_dump(mode="json")})  # fmt: skip

    late = await rfq_agent.fallback(ctx, rfq.session_id)  # before the hard deadline: no later price would meet it
    assert late.option.carrier == quote.option.carrier
    assert "outside the timing you requested" in late.body


@pytest.mark.parametrize("dry_run, late_mail", [(True, True), (False, False)])  # a dry run; a replay (the KPI)
async def test_no_wait_for_a_late_price_that_cannot_arrive(conn, monkeypatch, dry_run, late_mail):
    ctx = make_ctx(conn, dry_run=dry_run)
    ctx.mailbox.late_mail = late_mail
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx, before_hard_s=300)

    async def nothing(*args):
        return None

    monkeypatch.setattr(rfq_agent, "handle", nothing)
    monkeypatch.setattr(rfq_agent, "fallback", nothing)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, rfq), timeout=1)
    assert kinds(ctx)[-1] == "quote.missed"


async def test_one_quote_when_the_agent_answers_in_time(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx, before_hard_s=5)

    async def handle(ctx, message, deadline):
        return await outbox.send_quote(ctx, make_quote(rfq))

    async def fallback(ctx, session_id, *, allow_partial=False):
        raise AssertionError("no fallback after a sent quote")

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await orchestrator.quote_with_deadline(ctx, rfq)
    assert len(ctx.mailbox.sent) == 1
    assert "quote.fallback" not in kinds(ctx)


async def test_recover_restarts_an_open_request(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    conn.execute("UPDATE messages SET kind = 'customer_rfq' WHERE id = ?", (rfq.id,))
    fake_clock(monkeypatch, rfq, ctx)
    restarted = []

    async def quote_with_deadline(ctx, message):
        restarted.append(message.id)

    monkeypatch.setattr(orchestrator, "quote_with_deadline", quote_with_deadline)
    await orchestrator.recover(ctx)
    await asyncio.gather(*ctx.tasks)
    assert restarted == [rfq.id]


async def test_late_seen_request_still_asks_airlines_for_a_bid(conn, monkeypatch):
    """Seen 160 s after it was stored (a restart, a poll backlog): the handler keeps its turn, recovery asks no airline."""
    ctx = make_ctx(conn)  # live defaults: target 100 s, hard 315 s
    stored = datetime.now(UTC) - timedelta(seconds=160)  # 155 s left before hard_at
    msg = mail("Please quote AAA to BBB, 100 kg, 0.3 cbm.").model_copy(update={"sent_at": stored})
    store.save_carriers(
        ctx, [{"name": "Test Air", "kind": "airline", "email": "test-air@example.com", "serves": "BBB"}]
    )

    async def no_portal(*args, **kwargs):
        return []

    monkeypatch.setattr(portal_rates, "lanes", no_portal)
    monkeypatch.setattr(portal_rates, "options", no_portal)
    first = poller.ingest(ctx, msg)
    task = asyncio.create_task(orchestrator.dispatch(ctx, first))
    try:
        enquiry = None
        async with asyncio.timeout(3):
            while enquiry is None:
                await asyncio.sleep(0.01)
                enquiry = conn.execute("SELECT id FROM messages WHERE purpose='bid_request'").fetchone()
        response = msg.model_copy(update={"id": 100, "sender": "test-air@example.com", "subject": "Re: Rate request",
                                          "body": "Our all-in freight rate is EUR 2.80/kg. Transit 3 days.",
                                          "in_reply_to": enquiry[0], "sent_at": datetime.now(UTC)})  # fmt: skip
        await orchestrator.dispatch(ctx, poller.ingest(ctx, response))
        await asyncio.wait_for(task, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert quotes.for_session(ctx, msg.id).total == 280


class SlowGateway:
    def __init__(self):
        self.purposes = []

    async def chat(self, payload):
        self.purposes.append(payload["response_format"]["json_schema"]["name"])
        await asyncio.sleep(2)  # a slow model answer, still running at the takeover
        raise RuntimeError("gateway down")


async def test_takeover_does_not_repeat_the_extraction_call(conn):
    """The takeover cancels the model read before its `llm.call` is written; recovery must not read again."""
    base = make_ctx(conn)
    gateway = SlowGateway()
    ctx = Context(base.settings, conn, base.mailbox, None, gateway, None)
    stored = datetime.now(UTC) - timedelta(seconds=149)  # the takeover fires 1 s from now
    msg = mail("Hi, some cargo from our plant near Munich to a customer in the Chicago area, could you quote?")
    msg = poller.ingest(ctx, msg.model_copy(update={"sent_at": stored}))
    task = asyncio.create_task(orchestrator.quote_with_deadline(ctx, msg))
    try:
        await asyncio.sleep(4)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    calls = [p for p in gateway.purposes if p == "rfq_extract"]
    assert len(calls) == 1


async def test_a_sea_freight_request_gets_no_price_and_its_answer_at_once(conn, monkeypatch):
    """Round 177: "port-to-port sea freight ... FCL in 1 x 40ft container" was read as air HAM-DXB and quoted from
    the portal; the scorer rejected it as out_of_scope_quoted. We quote air only: no price, one plain answer."""
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx, "rfq-sea-freight-fcl.json")
    fake_clock(monkeypatch, message, ctx, before_hard_s=200)
    quote = make_quote(message)

    async def handle(ctx, message, deadline):
        events.emit(ctx, "rfq.extracted", "rfq", session_id=message.id, payload=quote.rfq.model_dump(mode="json"))
        events.emit(ctx, "option.found", "pricing", session_id=message.id,
                    payload={"option": quote.option.model_dump(mode="json")})  # fmt: skip

    monkeypatch.setattr(rfq_agent, "handle", handle)
    assert orchestrator.out_of_scope(ctx, message.id)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, message), timeout=1)
    assert "quote.sent" not in kinds(ctx)
    assert ctx.mailbox.sent == [message.sender]
    reply = conn.execute("SELECT body FROM messages WHERE purpose='no_price'").fetchone()["body"]
    assert "air export freight only" in reply  # why, not "no rate in time"
    assert not orchestrator.out_of_scope(ctx, ingest_rfq(ctx).id)


@pytest.mark.parametrize(
    ("text", "declined"),
    [
        ("Please quote port-to-port sea freight HAM to DXB, FCL 1 x 40ft.", True),
        ("Bitte Seefracht HAM nach DXB, 1 x 40ft FCL.", True),
        ("Cotation fret maritime LCL Le Havre - Dubai, 4 cbm.", True),
        # an air request that names the other mode only to set it aside is quoted
        ("Quote FRA to JFK 300 kg please, not by sea.", False),
        ("We usually ship by sea, but this one is urgent: FRA to JFK, 300 kg, 1.2 cbm.", False),
        ("Our sea freight is too slow for this order. Please quote FRA - JFK, 300 kg, 1.2 cbm.", False),
        ("Bitte Angebot FRA nach JFK, 300 kg. Seefracht dauert uns zu lange.", False),
        ("Merci de coter FRA - JFK, 300 kg, fret aérien, pas de LCL.", False),
    ],
)
def test_only_a_request_for_another_mode_is_out_of_scope(conn, text, declined):
    ctx = make_ctx(conn)
    conn.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (1, 1, 'in', 'c@x', 'us@x', 'Request', ?, '', '{}')",
        (text,),
    )
    assert orchestrator.out_of_scope(ctx, 1) is declined
