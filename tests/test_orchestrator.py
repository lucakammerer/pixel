import asyncio
from datetime import timedelta

from pixel.agents.rfq import agent as rfq_agent
from pixel.core import orchestrator, outbox
from tests.test_outbox import ingest_rfq, kinds, make_ctx, make_quote


def fake_clock(monkeypatch, rfq, ctx, before_hard_s=0.05):
    hard_at = orchestrator.deadline_for(rfq.sent_at, ctx.settings).hard_at
    monkeypatch.setattr(orchestrator, "now", lambda: hard_at - timedelta(seconds=before_hard_s))


async def test_fallback_sends_at_hard_at_when_the_agent_hangs(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx)
    hung = asyncio.Event()

    async def handle(ctx, message, deadline):
        await hung.wait()

    async def fallback(ctx, session_id):
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

    async def fallback(ctx, session_id):
        return None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await orchestrator.quote_with_deadline(ctx, rfq)
    assert ctx.mailbox.sent == []
    assert kinds(ctx)[-1] == "quote.missed"


async def test_an_agent_that_gives_up_early_still_quotes_a_price_that_arrives_before_hard_at(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx, before_hard_s=1.0)
    monkeypatch.setattr(orchestrator, "FALLBACK_POLL_S", 0.01)
    calls = []

    async def handle(ctx, message, deadline):
        return None  # no price yet: a late bid is still on its way

    async def fallback(ctx, session_id):
        calls.append(session_id)
        return make_quote(rfq, "Q-late") if len(calls) >= 3 else None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, rfq), timeout=1)
    assert len(ctx.mailbox.sent) == 1
    assert "quote.missed" not in kinds(ctx)
    assert kinds(ctx).count("quote.waiting") == 1


async def test_one_quote_when_the_agent_answers_in_time(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    fake_clock(monkeypatch, rfq, ctx, before_hard_s=5)

    async def handle(ctx, message, deadline):
        return await outbox.send_quote(ctx, make_quote(rfq))

    async def fallback(ctx, session_id):
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
