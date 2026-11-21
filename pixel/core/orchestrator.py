"""Routes each mail to its handler and holds every request to its own deadline."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pixel.config import Settings
from pixel.contracts import MailKind, Message
from pixel.core import outbox
from pixel.core.context import Context
from pixel.core.events import emit

EXPIRES_AFTER = timedelta(minutes=6)


@dataclass(frozen=True)
class Deadline:
    received_at: datetime
    target_at: datetime  # send by here for full time points
    hard_at: datetime  # the fallback sends whatever we have
    expires_at: datetime


def deadline_for(received_at: datetime, settings: Settings) -> Deadline:
    return Deadline(
        received_at=received_at,
        target_at=received_at + timedelta(seconds=settings.quote_target_s),
        hard_at=received_at + timedelta(seconds=settings.quote_hard_s),
        expires_at=received_at + EXPIRES_AFTER,
    )


def now() -> datetime:
    return datetime.now(UTC)


def _error(ctx: Context, where: str, e: BaseException, message: Message) -> None:
    emit(ctx, "error", "orchestrator", session_id=message.session_id, message_id=message.id,
         payload={"where": where, "error": f"{type(e).__name__}: {e}"})  # fmt: skip


async def dispatch(ctx: Context, message: Message) -> None:
    """router.classify, then: customer_rfq -> quote_with_deadline; customer_reply -> rfq.booking.handle_reply;
    carrier_bid, ratedesk_reply -> pricing.bids.record; booking_confirmation -> rfq.booking.handle_confirmation;
    tariff -> tariffs.handle; tracking_question -> tracking.handle. An exception becomes an `error` event."""
    from pixel.agents.rfq import booking
    from pixel.agents.router import agent as router
    from pixel.agents.tariffs import agent as tariffs
    from pixel.agents.tracking import agent as tracking
    from pixel.pricing import bids

    try:
        kind = await router.classify(ctx, message)
    except Exception as e:  # noqa: BLE001
        _error(ctx, "route", e, message)
        kind = MailKind.OTHER if message.in_reply_to or message.references else MailKind.CUSTOMER_RFQ
    ctx.db.execute("UPDATE messages SET kind = ? WHERE id = ?", (kind.value, message.id))
    message = message.model_copy(update={"kind": kind})
    handlers = {
        MailKind.CUSTOMER_RFQ: lambda: quote_with_deadline(ctx, message),
        MailKind.CUSTOMER_REPLY: lambda: booking.handle_reply(ctx, message),
        MailKind.CARRIER_BID: lambda: bids.record(ctx, message),
        MailKind.RATEDESK_REPLY: lambda: bids.record(ctx, message),
        MailKind.BOOKING_CONFIRMATION: lambda: booking.handle_confirmation(ctx, message),
        MailKind.TARIFF: lambda: tariffs.handle(ctx, message),
        MailKind.TRACKING_QUESTION: lambda: tracking.handle(ctx, message),
    }
    handler = handlers.get(kind)
    if handler is None:
        return
    try:
        await handler()
    except Exception as e:  # noqa: BLE001
        _error(ctx, kind.value, e, message)


def _open_session(ctx: Context, message: Message, deadline: Deadline) -> None:
    ctx.db.execute(
        "INSERT OR IGNORE INTO sessions (id, customer, state, received_at, deadline_at) VALUES (?, ?, 'open', ?, ?)",
        (message.session_id, message.sender, deadline.received_at.isoformat(), deadline.expires_at.isoformat()),
    )


async def quote_with_deadline(ctx: Context, message: Message) -> None:
    """Runs rfq.agent.handle as a task. If no quote is sent by `hard_at`, sends rfq.agent.fallback's quote and
    cancels the task; if that has no price either, emits `quote.missed`. The deadline runs from the mail's stored
    time, not from when we polled it (R1)."""
    from pixel.agents.rfq import agent as rfq

    deadline = deadline_for(message.sent_at, ctx.settings)
    _open_session(ctx, message, deadline)
    session_id = message.session_id
    task = asyncio.ensure_future(rfq.handle(ctx, message, deadline))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, (deadline.hard_at - now()).total_seconds()))
    except TimeoutError:
        pass
    except outbox.NotSent:
        pass
    except Exception as e:  # noqa: BLE001
        _error(ctx, "rfq", e, message)
    if outbox.quoted(ctx, session_id):
        return
    task.cancel()
    await _fallback(ctx, message, deadline)


async def _fallback(ctx: Context, message: Message, deadline: Deadline) -> None:
    from pixel.agents.rfq import agent as rfq

    session_id = message.session_id
    if now() >= deadline.expires_at:
        _missed(ctx, session_id, "expired")
        return
    try:
        quote = await rfq.fallback(ctx, session_id)
    except Exception as e:  # noqa: BLE001
        _error(ctx, "fallback", e, message)
        quote = None
    if quote is None:
        _missed(ctx, session_id, "no_price")
        return
    quote = quote.model_copy(update={"fallback": True})
    try:
        await outbox.send_quote(ctx, quote)
    except outbox.NotSent as e:
        if e.reason != "dry_run" and e.reason != "duplicate_quote":
            _missed(ctx, session_id, e.reason)
            return
    except Exception as e:  # noqa: BLE001
        _error(ctx, "fallback_send", e, message)
        _missed(ctx, session_id, "send_failed")
        return
    emit(ctx, "quote.fallback", "orchestrator", session_id=session_id,
         payload={"quote": quote.id, "total": str(quote.total), "currency": quote.currency})  # fmt: skip


def _missed(ctx: Context, session_id: int, reason: str) -> None:
    ctx.db.execute("UPDATE sessions SET state = 'missed' WHERE id = ?", (session_id,))
    emit(ctx, "quote.missed", "orchestrator", session_id=session_id, payload={"reason": reason})


async def recover(ctx: Context) -> None:
    """After a restart: every session still inside its deadline without a sent quote gets its task back, and every
    stored mail that was never routed is dispatched again. Returns at once; the work runs as tasks."""
    from pixel.core.poller import spawn

    cutoff = (now() - EXPIRES_AFTER).isoformat()
    rows = ctx.db.execute(
        "SELECT m.data, m.kind FROM messages m LEFT JOIN sessions s ON s.id = m.session_id AND m.kind = ?"
        " WHERE m.direction = 'in' AND m.sent_at > ? AND (m.kind IS NULL OR (m.kind = ? AND s.state = 'open'))"
        " ORDER BY m.id",
        (MailKind.CUSTOMER_RFQ.value, cutoff, MailKind.CUSTOMER_RFQ.value),
    ).fetchall()
    for row in rows:
        message = Message.model_validate_json(row["data"])
        if row["kind"] is None:
            spawn(ctx, dispatch(ctx, message))
        elif not outbox.quoted(ctx, message.session_id):
            spawn(ctx, quote_with_deadline(ctx, message.model_copy(update={"kind": MailKind.CUSTOMER_RFQ})))
