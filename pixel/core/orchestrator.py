"""Routes each mail to its handler and holds every request to its own deadline."""

import asyncio
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

from pixel.config import Settings
from pixel.contracts import MailKind, Message, Quote
from pixel.core import outbox
from pixel.core.context import Context
from pixel.core.events import emit
from pixel.core.timing import now

EXPIRES_AFTER = timedelta(minutes=6)
TAKE_OVER_AFTER = timedelta(seconds=150)
FALLBACK_POLL_S = 2.0


@dataclass(frozen=True)
class Deadline:
    received_at: datetime
    target_at: datetime  # send by here for full time points
    hard_at: datetime  # the fallback sends whatever we have
    expires_at: datetime

    @property
    def recovery_at(self) -> datetime:
        return min(self.received_at + TAKE_OVER_AFTER, self.hard_at)


def deadline_for(received_at: datetime, settings: Settings) -> Deadline:
    return Deadline(
        received_at=received_at,
        target_at=received_at + timedelta(seconds=settings.quote_target_s),
        hard_at=received_at + timedelta(seconds=settings.quote_hard_s),
        expires_at=received_at + EXPIRES_AFTER,
    )


def _describe(e: BaseException) -> str:
    """A TaskGroup's error names only how many failed: each leaf with the line it was raised in."""
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe(leaf) for leaf in e.exceptions)
    frame = traceback.extract_tb(e.__traceback__)[-1:] if e.__traceback__ else []
    where = f" at {frame[0].filename.rsplit('/', 1)[-1]}:{frame[0].lineno}" if frame else ""
    return f"{type(e).__name__}: {e}{where}"


def _error(ctx: Context, where: str, e: BaseException, message: Message) -> None:
    emit(ctx, "error", "orchestrator", session_id=message.session_id, message_id=message.id,
         payload={"where": where, "error": _describe(e)})  # fmt: skip


async def dispatch(ctx: Context, message: Message) -> None:
    """router.classify, then: customer_rfq -> quote_with_deadline; customer_reply -> rfq.booking.handle_reply;
    carrier_bid, ratedesk_reply -> pricing.bids.record; booking_confirmation -> rfq.booking.handle_confirmation;
    tariff -> tariffs.handle; tracking_question -> tracking.handle. An exception becomes an `error` event."""
    from pixel.agents.rfq import booking
    from pixel.agents.router import agent as router
    from pixel.agents.tariffs import agent as tariffs
    from pixel.agents.tracking import agent as tracking
    from pixel.pricing import bids

    done = ctx.db.execute("SELECT completed FROM mail_work WHERE message_id=?", (message.id,)).fetchone()
    if done and done["completed"]:
        return
    message = await outbox.reconcile_reply(ctx, message)
    stored = ctx.db.execute("SELECT kind FROM messages WHERE id = ?", (message.id,)).fetchone()
    if stored and stored["kind"]:
        kind = MailKind(stored["kind"])
    else:
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
    try:
        if handler:
            await handler()
    except Exception as e:  # noqa: BLE001
        _error(ctx, kind.value, e, message)
        return
    ctx.db.execute("UPDATE mail_work SET completed = 1 WHERE message_id = ?", (message.id,))


def _open_session(ctx: Context, message: Message, deadline: Deadline) -> None:
    ctx.db.execute(
        "INSERT OR IGNORE INTO sessions (id, customer, state, received_at, deadline_at) VALUES (?, ?, 'open', ?, ?)",
        (message.session_id, message.sender, deadline.received_at.isoformat(), deadline.expires_at.isoformat()),
    )


async def quote_with_deadline(ctx: Context, message: Message) -> None:
    """Hand an unanswered request to recovery when the RFQ task ends or the recovery timer fires."""
    from pixel.agents.rfq import agent as rfq

    deadline = deadline_for(message.sent_at, ctx.settings)
    _open_session(ctx, message, deadline)
    session_id = message.session_id
    if outbox.quoted(ctx, session_id):
        return
    if now() >= deadline.expires_at:
        _missed(ctx, session_id, "expired")
        return
    task = asyncio.create_task(rfq.handle(ctx, message, deadline))
    reason = "handler_finished"
    try:
        # A request first seen after the takeover time (a restart, a poll backlog) keeps its handler until `hard_at`:
        # recovery asks no airline, so a takeover at once would leave a bid-only lane without a price.
        started = now()
        recovery_at = deadline.recovery_at if started <= deadline.recovery_at else deadline.hard_at
        timeout = max(0.0, (recovery_at - started).total_seconds())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if done:
            task.result()
        else:
            reason = "timeout"
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except outbox.NotSent:
        pass
    except Exception as e:  # noqa: BLE001
        reason = "handler_error"
        _error(ctx, "rfq", e, message)
    if outbox.quoted(ctx, session_id):
        # A POST can still be in flight at handoff. Keep reading its receipt until expiry.
        if not task.done():
            try:
                await asyncio.wait_for(task, max(0.0, (deadline.expires_at - now()).total_seconds()))
            except Exception as e:  # noqa: BLE001  track any failed delivery
                _error(ctx, "quote_delivery", e, message)
        if outbox.quoted(ctx, session_id):
            return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if not outbox.taken_over(ctx, session_id) and now() < deadline.hard_at:
        emit(ctx, "quote.recovery", "orchestrator", session_id=session_id, payload={"reason": reason})
    await _fallback(ctx, message, deadline)


async def _fallback(ctx: Context, message: Message, deadline: Deadline) -> None:
    session_id = message.session_id
    if now() >= deadline.expires_at:
        _missed(ctx, session_id, "expired")
        return
    quote = await _first_price(ctx, message, deadline)
    if outbox.quoted(ctx, session_id):
        return
    if quote is None:
        _missed(ctx, session_id, "no_price")
        await _no_price_reply(ctx, message, deadline)
        return
    quote = quote.model_copy(update={"fallback": True})
    try:
        async with asyncio.timeout(max(0.0, (deadline.expires_at - now()).total_seconds())):
            await outbox.send_quote(ctx, quote)
    except outbox.NotSent as e:
        if e.reason != "dry_run":
            if e.reason not in ("duplicate_quote", "superseded"):
                _missed(ctx, session_id, e.reason)
            return
    except Exception as e:  # noqa: BLE001
        _error(ctx, "fallback_send", e, message)
        if not outbox.quoted(ctx, session_id):
            _missed(ctx, session_id, "send_failed")
        return
    emit(ctx, "quote.fallback", "orchestrator", session_id=session_id,
         payload={"quote": quote.id, "total": str(quote.total), "currency": quote.currency,
                  "complete_service": not quote.rfq.door_pickup or bool(quote.option.trucker)})  # fmt: skip


async def _first_price(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """rfq.agent.fallback until it has a price, a person has quoted, or `hard_at` passes. An agent that gave up
    early leaves minutes on the clock in which a late bid, a rate desk answer or a person can still price the
    request: a miss is final, a late quote only costs time points (R1, R29)."""
    from pixel.agents.rfq import agent as rfq
    from pixel.core import quote_recovery
    from pixel.core.poller import spawn

    loop = asyncio.get_running_loop()
    # A dry run holds outgoing bids. Replays report whether a later captured reply can still arrive.
    late = not ctx.settings.dry_run and getattr(ctx.mailbox, "late_mail", True)
    wait = (deadline.hard_at - now()).total_seconds() if late else 0.0
    until = loop.time() + max(0.0, wait)
    failed = waiting = False
    sourcing = None
    try:
        while True:
            if outbox.quoted(ctx, message.session_id):
                return None
            at_deadline = loop.time() >= until
            human = outbox.taken_over(ctx, message.session_id)
            quote = None
            try:
                if at_deadline or not human:
                    quote = await rfq.fallback(ctx, message.session_id, allow_partial=at_deadline)
            except Exception as e:  # noqa: BLE001
                if not failed:
                    _error(ctx, "fallback", e, message)
                failed = True
            if quote is not None or at_deadline:
                return quote
            if not waiting:
                waiting = True
                emit(ctx, "quote.waiting", "orchestrator", session_id=message.session_id,
                     payload={"reason": "human" if human else "no_complete_price",
                              "until": deadline.hard_at.isoformat()})  # fmt: skip
            if sourcing is None and not human:
                sourcing = spawn(ctx, quote_recovery.source(ctx, message, deadline))
            await asyncio.sleep(min(FALLBACK_POLL_S, until - loop.time()))
    finally:
        if sourcing is not None:
            sourcing.cancel()


async def _no_price_reply(ctx: Context, message: Message, deadline: Deadline) -> None:
    """R1: a request no sheet, portal rate, bid or person priced by `hard_at` gets one plain answer, never silence.
    It holds no figure: a guessed price is wrong for the customer and the scorer alike."""
    from pixel.agents.rfq import compose
    from pixel.pricing import session

    session_id = message.session_id
    rfq = session.rfq(ctx, session_id)
    if ctx.settings.dry_run or not getattr(ctx.mailbox, "late_mail", True):
        return
    left = (deadline.expires_at - now()).total_seconds()
    try:
        async with asyncio.timeout(max(0.0, left)):
            subject, body = compose.no_price(ctx, message, rfq)
            await outbox.send(ctx, to=message.sender, subject=subject, body=body, session_id=session_id,
                                     in_reply_to=message.id, purpose="no_price")  # fmt: skip
    except outbox.NotSent:
        return
    except Exception as e:  # noqa: BLE001
        _error(ctx, "no_price_reply", e, message)
        return


def _missed(ctx: Context, session_id: int, reason: str) -> None:
    ctx.db.execute("UPDATE sessions SET state = 'missed' WHERE id = ?", (session_id,))
    emit(ctx, "quote.missed", "orchestrator", session_id=session_id, payload={"reason": reason})


async def recover(ctx: Context) -> None:
    """After a restart: every session still inside its deadline without a sent quote gets its task back, and every
    stored mail that was never routed is dispatched again. Returns at once; the work runs as tasks."""
    from pixel.core.poller import spawn

    outbox.repair_receipts(ctx)
    cutoff = (now() - EXPIRES_AFTER).isoformat()
    rows = ctx.db.execute(
        "SELECT m.data FROM messages m LEFT JOIN mail_work w ON w.message_id=m.id "
        "LEFT JOIN sessions s ON s.id=m.session_id WHERE m.direction='in' AND (w.completed=0 OR "
        "(w.message_id IS NULL AND m.sent_at>? AND (m.kind IS NULL OR (m.kind=? AND s.state='open') OR "
        "(m.kind=? AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.direction='out' AND r.in_reply_to=m.id "
        "AND r.purpose='tracking') AND NOT EXISTS (SELECT 1 FROM events e WHERE e.session_id=m.session_id "
        "AND e.kind='mail.held' AND json_extract(e.payload,'$.in_reply_to')=m.id))))) ORDER BY m.id",
        (cutoff, MailKind.CUSTOMER_RFQ.value, MailKind.TRACKING_QUESTION.value),
    ).fetchall()
    for row in rows:
        message = Message.model_validate_json(row["data"])
        ctx.db.execute("INSERT OR IGNORE INTO mail_work(message_id) VALUES (?)", (message.id,))
        spawn(ctx, dispatch(ctx, message))
