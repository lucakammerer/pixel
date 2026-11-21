"""The only way mail leaves. Enforces first-quote-only, the recipient list, the mail limit, dry run and takeover."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.contracts import Message, Quote, QuoteStatus
from pixel.core.context import Context
from pixel.core.events import emit, scrub

MAILS_PER_MINUTE = 60  # R7
HUMAN = "human"  # purpose of mail a person sends from the UI: passes a takeover


class NotSent(Exception):
    """Refused before the API was called: `reason` is dry_run, human, duplicate_quote or recipient."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def allowed(ctx: Context, address: str) -> bool:
    """R5: customers who wrote to us, carriers of the carrier list, the rate desk and tracking."""
    address = address.strip().lower()
    if address in (RATEDESK, TRACKING):
        return True
    if ctx.db.execute("SELECT 1 FROM messages WHERE direction = 'in' AND sender = ?", (address,)).fetchone():
        return True
    return ctx.db.execute("SELECT 1 FROM carriers WHERE lower(email) = ?", (address,)).fetchone() is not None


def taken_over(ctx: Context, session_id: int | None) -> bool:
    if session_id is None:
        return False
    row = ctx.db.execute("SELECT mode FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return bool(row and row["mode"] == "human")


def quoted(ctx: Context, session_id: int) -> bool:
    """True once a quote for the session was sent, held by dry run, or is being sent."""
    return ctx.db.execute("SELECT 1 FROM kv WHERE key = ?", (f"quote:{session_id}",)).fetchone() is not None


async def _wait_for_slot(ctx: Context) -> None:
    while True:
        now = datetime.now(UTC)
        while ctx.mail_times and now - ctx.mail_times[0] >= timedelta(minutes=1):
            ctx.mail_times.popleft()
        if len(ctx.mail_times) < MAILS_PER_MINUTE:
            ctx.mail_times.append(now)
            return
        await asyncio.sleep((ctx.mail_times[0] + timedelta(minutes=1) - now).total_seconds())


def _store(ctx: Context, message: Message, purpose: str) -> None:
    ctx.db.execute(
        "INSERT OR IGNORE INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
        " in_reply_to, sent_at, data) VALUES (?, ?, 'out', NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message.id, message.session_id, purpose, message.sender, message.to, message.subject, message.body,
         message.in_reply_to, message.sent_at.isoformat(), message.model_dump_json()),
    )  # fmt: skip


async def _deliver(ctx: Context, to: str, subject: str, body: str, in_reply_to: int | None) -> Message:
    """Waits out the mail limit and every 429; never drops a mail."""
    while True:
        await _wait_for_slot(ctx)
        try:
            return await ctx.mailbox.send(to, subject, body, in_reply_to)
        except ApiError as e:
            if e.status != 429:
                raise
            await asyncio.sleep(e.retry_after or 1.0)


async def send(
    ctx: Context,
    *,
    to: str,
    subject: str,
    body: str,
    session_id: int | None,
    in_reply_to: int | None = None,
    purpose: str,
) -> Message:
    """Sends, stores the mail, emits `mail.out` (or `mail.held` and raises NotSent).
    `purpose` is stored with the mail; the router reads it to place answers (`bid_request`, `quote`, ...)."""
    to = to.strip().lower()
    held = {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to, "purpose": purpose}
    reason = None
    if not allowed(ctx, to):
        reason = "recipient"
    elif ctx.settings.dry_run:
        reason = "dry_run"
    elif (
        purpose not in (HUMAN, "fallback_quote", "ratedesk")
        and not purpose.startswith("bid")
        and taken_over(ctx, session_id)
    ):
        reason = HUMAN
    if reason:
        emit(ctx, "mail.held", "outbox", session_id=session_id, payload={**held, "reason": reason})
        raise NotSent(reason)
    try:
        sent = await _deliver(ctx, to, subject, body, in_reply_to)
    except Exception as e:
        emit(ctx, "error", "outbox", session_id=session_id, payload={**held, "error": scrub(ctx, str(e))})
        raise
    sent = sent.model_copy(update={"session_id": session_id if session_id is not None else sent.id})
    _store(ctx, sent, purpose)
    emit(ctx, "mail.out", "outbox", session_id=sent.session_id, message_id=sent.id, payload=held)
    return sent


async def send_quote(ctx: Context, quote: Quote) -> Quote:
    """Replies to the customer's RFQ mail. Atomic per session: a second quote for the same session raises NotSent.
    R4: only our first quote in a thread counts, so the claim is taken before the first await."""
    if not quote.fallback and taken_over(ctx, quote.session_id):
        emit(ctx, "mail.held", "outbox", session_id=quote.session_id, payload={"reason": HUMAN, "quote": quote.id})
        raise NotSent(HUMAN)
    try:
        ctx.db.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (f"quote:{quote.session_id}", quote.id))
    except sqlite3.IntegrityError:
        raise NotSent("duplicate_quote") from None
    rfq = ctx.db.execute("SELECT sender FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
    to = rfq["sender"] if rfq else quote.rfq.customer
    try:
        sent = await send(
            ctx,
            to=to,
            subject=quote.subject,
            body=quote.body,
            session_id=quote.session_id,
            in_reply_to=quote.rfq.message_id,
            purpose="fallback_quote" if quote.fallback else "quote",
        )
    except NotSent as e:
        if e.reason != "dry_run":  # a held dry-run quote keeps the claim, so dry and live runs behave alike
            ctx.db.execute("DELETE FROM kv WHERE key = ?", (f"quote:{quote.session_id}",))
        raise
    except Exception:
        ctx.db.execute("DELETE FROM kv WHERE key = ?", (f"quote:{quote.session_id}",))
        raise
    ctx.db.execute(
        "UPDATE sessions SET state = 'quoted', quote_message_id = ? WHERE id = ?", (sent.id, quote.session_id)
    )
    quote = quote.model_copy(update={"status": QuoteStatus.SENT, "sent_message_id": sent.id, "sent_at": sent.sent_at})
    emit(
        ctx,
        "quote.sent",
        "outbox",
        session_id=quote.session_id,
        message_id=sent.id,
        payload={"quote": quote.id, "total": str(quote.total), "currency": quote.currency, "fallback": quote.fallback,
                 "source": quote.option.source, "carrier": quote.option.carrier},
    )  # fmt: skip
    return quote
