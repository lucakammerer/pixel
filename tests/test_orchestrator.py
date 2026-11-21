import asyncio
from datetime import timedelta

import pytest

from pixel.agents.rfq import agent as rfq_agent
from pixel.core import events, orchestrator, outbox, quote_recovery
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

    async def fallback(ctx, session_id, *, allow_partial=False):
        calls.append(session_id)
        return make_quote(rfq, "Q-late") if len(calls) >= 3 else None

    monkeypatch.setattr(rfq_agent, "handle", handle)
    monkeypatch.setattr(rfq_agent, "fallback", fallback)
    await asyncio.wait_for(orchestrator.quote_with_deadline(ctx, rfq), timeout=1)
    assert len(ctx.mailbox.sent) == 1
    assert "quote.missed" not in kinds(ctx)
    assert kinds(ctx).count("quote.waiting") == 1


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
