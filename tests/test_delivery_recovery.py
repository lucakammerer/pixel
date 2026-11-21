import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from pixel.agents.rfq import agent, booking
from pixel.agents.tracking import agent as tracking
from pixel.api.client import ApiError
from pixel.api.mailbox import TRACKING, to_message
from pixel.contracts import QuoteStatus
from pixel.core import orchestrator, outbox, poller, timing
from pixel.crm import quotes
from tests.test_booking import Portal
from tests.test_outbox import FakeMailbox, ingest_rfq, make_ctx, make_quote


async def test_restart_before_a_slot_keeps_the_rfq_eligible(conn, monkeypatch):
    ctx = make_ctx(conn)
    message = ingest_rfq(ctx)
    conn.execute("UPDATE messages SET kind='customer_rfq'")
    ctx.mail_times.extend([timing.now()] * outbox.MAILS_PER_MINUTE)
    blocked = asyncio.create_task(outbox.send_quote(ctx, make_quote(message)))
    await asyncio.sleep(0)
    blocked.cancel()
    await asyncio.gather(blocked, return_exceptions=True)
    assert not ctx.mailbox.sent and not outbox.quoted(ctx, message.id)

    ctx.mail_times.clear()

    async def resume(ctx, msg):
        await outbox.send_quote(ctx, make_quote(msg, fallback=True))

    monkeypatch.setattr(orchestrator, "quote_with_deadline", resume)
    await orchestrator.recover(ctx)
    await asyncio.gather(*ctx.tasks)
    assert ctx.mailbox.sent == [message.sender]
    assert quotes.for_session(ctx, message.id).sent_at


@pytest.mark.parametrize("error", [ApiError(0, "ReadTimeout"), ApiError(503, "unavailable")])
async def test_a_quote_whose_send_failed_never_blocks_the_next_quote(conn, error):
    """A lost or refused quote must leave the fallback free to answer (R1); a second quote is harmless (R4)."""

    class Failing(FakeMailbox):
        failed = False

        async def send(self, *args):
            if not self.failed:
                self.failed = True
                raise error
            return await super().send(*args)

    ctx = make_ctx(conn, Failing())
    msg = ingest_rfq(ctx)
    with pytest.raises(ApiError):
        await outbox.send_quote(ctx, make_quote(msg))
    assert not outbox.quoted(ctx, msg.id)
    await outbox.send_quote(ctx, make_quote(msg, "Q-fallback", fallback=True))
    assert len(ctx.mailbox.sent) == 1 and outbox.quoted(ctx, msg.id)


async def test_fallback_receipt_can_be_booked_once(conn, monkeypatch):
    ctx = make_ctx(conn)
    ctx.portal = Portal(None)
    msg = ingest_rfq(ctx)
    quote = make_quote(msg)
    quote = quote.model_copy(update={"option": quote.option.model_copy(update={"rate_id": "test-rate"})})

    async def finished(*args):
        return None

    monkeypatch.setattr(agent, "handle", finished)
    monkeypatch.setattr(agent, "fallback", AsyncMock(return_value=quote))
    monkeypatch.setattr(orchestrator, "now", lambda: msg.sent_at)
    await orchestrator.quote_with_deadline(ctx, msg)
    sent = quotes.for_session(ctx, msg.id)
    assert sent.status == QuoteStatus.SENT and sent.fallback and sent.sent_message_id
    reply = msg.model_copy(
        update={
            "id": msg.id + 1,
            "subject": "Re: quote",
            "body": "Please book it.",
            "in_reply_to": sent.sent_message_id,
        }
    )
    await booking.handle_reply(ctx, reply)
    await booking.handle_reply(ctx, reply)
    assert len(ctx.portal.calls) == 1


async def test_operator_keeps_the_review_window_and_must_send_the_quote_first(conn, monkeypatch):
    ctx = make_ctx(conn)
    msg = ingest_rfq(ctx)
    conn.execute("UPDATE sessions SET mode='human'")
    with pytest.raises(outbox.NotSent, match="priced quote"):
        await outbox.send(ctx, to=msg.sender, subject="Re:", body="Thank you", session_id=msg.id, purpose="human")
    monkeypatch.setattr(agent, "handle", AsyncMock(side_effect=outbox.NotSent("human")))
    fallback = AsyncMock(return_value=make_quote(msg))
    monkeypatch.setattr(agent, "fallback", fallback)
    hard = orchestrator.deadline_for(msg.sent_at, ctx.settings).hard_at
    monkeypatch.setattr(orchestrator, "now", lambda: hard - timedelta(seconds=10))
    monkeypatch.setattr(orchestrator, "FALLBACK_POLL_S", 0.01)
    pending = asyncio.create_task(orchestrator.quote_with_deadline(ctx, msg))
    await asyncio.sleep(0.03)
    assert not ctx.mailbox.sent
    await outbox.send_quote(ctx, make_quote(msg), operator=True)
    await asyncio.wait_for(pending, 1)
    assert ctx.mailbox.sent == [msg.sender] and not fallback.await_count


async def test_recovery_dispatches_unfinished_tracking_and_reuses_a_delivered_reply(conn, monkeypatch):
    ctx = make_ctx(conn)
    msg = poller.ingest(
        ctx,
        to_message(
            {
                "id": 12,
                "from": TRACKING,
                "to": "us@x",
                "subject": "Status?",
                "body": "Please check",
                "received_at": timing.now().isoformat(),
            },
            "in",
        ),
    )
    conn.execute("UPDATE messages SET kind='tracking_question' WHERE id=?", (msg.id,))
    # Simulate a shutdown just after the first answer reached the provider and before the handler returned.
    await outbox.send(
        ctx,
        to=TRACKING,
        subject="Re: Status?",
        body="First answer",
        session_id=msg.id,
        in_reply_to=msg.id,
        purpose="tracking",
    )
    real = tracking.handle
    called = AsyncMock(side_effect=real)
    monkeypatch.setattr(tracking, "handle", called)
    await orchestrator.recover(ctx)
    await asyncio.gather(*ctx.tasks)
    await orchestrator.recover(ctx)
    await asyncio.gather(*ctx.tasks)
    assert called.await_count == 1
    assert ctx.mailbox.sent == [TRACKING]
    assert conn.execute("SELECT completed FROM mail_work WHERE message_id=?", (msg.id,)).fetchone()[0]


async def test_upgrade_recovers_a_legacy_fallback_draft_from_its_stored_mail(conn):
    ctx = make_ctx(conn)
    msg = ingest_rfq(ctx)
    quote = make_quote(msg, fallback=True)
    quotes.save(ctx, quote)
    conn.execute("INSERT INTO kv VALUES (?, ?)", (f"quote:{msg.id}", quote.id))
    sent = await ctx.mailbox.send(msg.sender, quote.subject, quote.body, msg.id)
    # Simulate the old process exiting after storing the outgoing mail and before updating the session.
    outbox._store(ctx, sent.model_copy(update={"session_id": msg.id}), "fallback_quote")
    outbox.repair_receipts(ctx)
    restored = quotes.for_session(ctx, msg.id)
    assert restored.status == QuoteStatus.SENT and restored.sent_message_id == sent.id
    assert ctx.mailbox.sent == [msg.sender]


def test_session_ui_refuses_an_acknowledgement_before_and_after_draft_creation(conn):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pixel.ui import routes

    ctx = make_ctx(conn)
    msg = ingest_rfq(ctx)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(routes.router)
    client = TestClient(app)
    form = {"to": msg.sender, "body": "Thank you, we are working on it.", "subject": "Re: request"}
    for has_draft in (False, True):
        if has_draft:
            quotes.save(ctx, make_quote(msg))
        response = client.post(f"/sessions/{msg.id}/send", data=form, follow_redirects=False)
        assert "error=" in response.headers["location"]
    assert not ctx.mailbox.sent


async def test_tracking_interrupted_before_send_restarts_even_after_six_minutes(conn, monkeypatch):
    ctx = make_ctx(conn)
    received = timing.now() - timedelta(minutes=12)
    msg = poller.ingest(
        ctx,
        to_message(
            {
                "id": 91,
                "from": TRACKING,
                "to": "us@x",
                "subject": "Status?",
                "body": "Please check",
                "received_at": received.isoformat(),
            },
            "in",
        ),
    )
    real = tracking.handle
    entered = asyncio.Event()

    async def interrupted(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(tracking, "handle", interrupted)
    task = asyncio.create_task(orchestrator.dispatch(ctx, msg))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not ctx.mailbox.sent
    assert conn.execute("SELECT kind FROM messages WHERE id=?", (msg.id,)).fetchone()[0] == "tracking_question"
    monkeypatch.setattr(tracking, "handle", real)
    await orchestrator.recover(ctx)
    await asyncio.gather(*ctx.tasks)
    assert ctx.mailbox.sent == [TRACKING]
