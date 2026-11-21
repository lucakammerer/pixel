"""The only way mail leaves. Enforces first-quote-only, the recipient list, the mail limit, dry run and takeover."""

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta

from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.contracts import Message, Quote, QuoteStatus
from pixel.core import db, timing
from pixel.core.context import Context
from pixel.core.events import emit, scrub
from pixel.crm import quotes

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
    key = f"quote:{session_id}"
    return bool(
        db.get(ctx.db, key) or ctx.db.execute("SELECT 1 FROM mail_delivery WHERE operation = ?", (key,)).fetchone()
    )


async def _wait_for_slot(ctx: Context) -> None:
    while True:
        now = timing.now()
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


def _sent_quote(ctx: Context, quote: Quote, sent: Message) -> Quote:
    status = QuoteStatus.SENT if quote.status == QuoteStatus.DRAFT else quote.status
    quote = quote.model_copy(
        update={
            "status": status,
            "sent_message_id": sent.id,
            "sent_at": sent.sent_at,
            "subject": sent.subject,
            "body": sent.body,
        }
    )
    quotes.save(ctx, quote)
    ctx.db.execute(
        "UPDATE sessions SET state = 'quoted', quote_message_id = ? WHERE id = ?", (sent.id, quote.session_id)
    )
    return quote


def _receipt(ctx: Context, operation: str, message: Message) -> Message:
    """The provider receipt, the event and the delivered quote become durable together."""
    with db.atomic(ctx.db):
        row = ctx.db.execute("SELECT * FROM mail_delivery WHERE operation = ?", (operation,)).fetchone()
        sent = message.model_copy(update={"direction": "out", "session_id": row["session_id"] or message.id})
        if row["sent_id"] is not None:
            return sent
        ctx.db.execute("UPDATE mail_delivery SET sent_id = ? WHERE operation = ?", (sent.id, operation))
        _store(ctx, sent, row["purpose"])
        emit(
            ctx,
            "mail.out",
            "outbox",
            session_id=sent.session_id,
            message_id=sent.id,
            payload={
                "to": sent.to,
                "subject": sent.subject,
                "body": sent.body,
                "purpose": row["purpose"],
                "in_reply_to": sent.in_reply_to,
            },
        )
        if row["quote_id"]:
            quote = _sent_quote(ctx, quotes.get(ctx, row["quote_id"]), sent)
            emit(
                ctx,
                "quote.sent",
                "outbox",
                session_id=quote.session_id,
                message_id=sent.id,
                payload={
                    "quote": quote.id,
                    "total": str(quote.total),
                    "currency": quote.currency,
                    "fallback": quote.fallback,
                    "source": quote.option.source,
                    "carrier": quote.option.carrier,
                    "complete_service": not quote.rfq.door_pickup or bool(quote.option.trucker),
                },
            )
    return sent


def _claimed(ctx: Context, key: str, is_quote: bool) -> Message | None:
    row = ctx.db.execute(
        "SELECT d.sent_id, m.data FROM mail_delivery d LEFT JOIN messages m ON m.id=d.sent_id WHERE d.operation=?",
        (key,),
    ).fetchone()
    if row:
        if is_quote:
            raise NotSent("duplicate_quote")
        if row["data"]:
            return Message.model_validate_json(row["data"])
        raise NotSent("delivery_unconfirmed")
    if is_quote and db.get(ctx.db, key):  # an older release's quote claim or a dry-run quote
        raise NotSent("duplicate_quote")
    return None


def _hold(ctx: Context, to: str, sid: int | None, purpose: str, quote: Quote | None, operator: bool) -> str | None:
    if not allowed(ctx, to):
        return "recipient"
    if (
        not operator
        and taken_over(ctx, sid)
        and not (purpose.startswith("bid") or purpose in (HUMAN, "fallback_quote", "ratedesk"))
    ):
        return HUMAN
    if (
        purpose == HUMAN
        and sid is not None
        and ctx.db.execute(
            "SELECT 1 FROM messages m LEFT JOIN sessions s ON s.id=m.session_id "
            "WHERE m.id=? AND m.direction='in' AND lower(m.sender)=? "
            "AND (s.id IS NOT NULL OR m.kind='customer_rfq' OR m.kind IS NULL) AND s.quote_message_id IS NULL",
            (sid, to),
        ).fetchone()
    ):
        return "send the priced quote before any other customer reply"
    if ctx.settings.dry_run:
        if quote:
            db.put(ctx.db, f"quote:{sid}", quote.id)
        return "dry_run"
    return None


async def _deliver(
    ctx: Context,
    to: str,
    subject: str,
    body: str,
    sid: int | None,
    parent: int | None,
    purpose: str,
    *,
    quote: Quote | None = None,
    operator: bool = False,
) -> Message:
    to = to.strip().lower()
    # Reply to a particular tracking question exactly once, even if its CRM data changes during a restart.
    key = json.dumps([sid, purpose, to, parent, "" if purpose == "tracking" else body], ensure_ascii=False)
    if quote:
        key = f"quote:{sid}"
    elif purpose == HUMAN:
        key = uuid.uuid4().hex
    else:
        key = hashlib.sha256(key.encode()).hexdigest()
    payload = {"to": to, "subject": subject, "body": body, "in_reply_to": parent, "purpose": purpose}
    while True:
        if sent := _claimed(ctx, key, quote is not None):
            return sent
        reason = _hold(ctx, to, sid, purpose, quote, operator)
        if reason:
            emit(ctx, "mail.held", "outbox", session_id=sid, payload={**payload, "reason": reason})
            raise NotSent(reason)
        await _wait_for_slot(ctx)
        # While waiting another sender may have won the quote or a person may have taken over.
        if sent := _claimed(ctx, key, quote is not None):
            return sent
        if reason := _hold(ctx, to, sid, purpose, quote, operator):
            emit(ctx, "mail.held", "outbox", session_id=sid, payload={**payload, "reason": reason})
            raise NotSent(reason)
        with db.atomic(ctx.db):
            if quote:
                quotes.save(ctx, quote)
            ctx.db.execute(
                "INSERT INTO mail_delivery(operation, session_id, purpose, recipient, subject, body, parent_id, quote_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, sid, purpose, to, subject, body, parent, quote.id if quote else None),
            )
        try:
            sent = await ctx.mailbox.send(to, subject, body, parent)
        except ApiError as e:
            # Rejection, or failure before connecting, definitely produced no mail. A lost POST response is ambiguous,
            # except for a quote: only the first quote counts (R4), so a second one is harmless, a lost one is not.
            if (
                quote is not None
                or 400 <= e.status < 500
                or e.status == 0 and e.detail in {"ConnectError", "ConnectTimeout", "PoolTimeout"}
            ):
                ctx.db.execute("DELETE FROM mail_delivery WHERE operation = ?", (key,))
            if e.status == 429:
                await asyncio.sleep(e.retry_after or 1.0)
                continue
            emit(
                ctx,
                "error",
                "outbox",
                session_id=sid,
                payload={**payload, "where": "send", "error": scrub(ctx, str(e))},
            )
            raise
        except (Exception, asyncio.CancelledError) as e:
            if quote is not None:  # the fallback must still be able to answer (R1); a duplicate quote is harmless
                ctx.db.execute("DELETE FROM mail_delivery WHERE operation = ?", (key,))
            emit(
                ctx,
                "error",
                "outbox",
                session_id=sid,
                payload={"where": "delivery_unconfirmed", "purpose": purpose, "error": type(e).__name__},
            )
            raise
        return _receipt(ctx, key, sent)


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
    """Send a non-quote. A replay of a delivered automatic action reuses its receipt."""
    return await _deliver(ctx, to, subject, body, session_id, in_reply_to, purpose)


async def send_quote(ctx: Context, quote: Quote, *, operator: bool = False) -> Quote:
    """Claim the session immediately before calling the mailbox and persist the returned receipt."""
    row = ctx.db.execute("SELECT sender FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
    to = row["sender"] if row else quote.rfq.customer
    await _deliver(
        ctx,
        to,
        quote.subject,
        quote.body,
        quote.session_id,
        quote.rfq.message_id,
        "fallback_quote" if quote.fallback else "quote",
        quote=quote,
        operator=operator,
    )
    return quotes.get(ctx, quote.id)


def repair_receipts(ctx: Context) -> None:
    """Upgrade confirmed legacy fallback drafts and preserve ambiguous legacy claims until a receipt arrives."""
    rows = ctx.db.execute(
        "SELECT k.key, q.data, s.quote_message_id FROM kv k JOIN quotes q ON q.id=k.value "
        "JOIN sessions s ON s.id=q.session_id WHERE k.key='quote:'||s.id AND q.sent_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM mail_delivery d WHERE d.operation=k.key)"
    ).fetchall()
    for row in rows:
        quote = Quote.model_validate_json(row["data"])
        mail = ctx.db.execute(
            "SELECT data FROM messages WHERE direction='out' AND session_id=? AND "
            "(id=? OR (purpose IN ('quote','fallback_quote') AND in_reply_to=?)) ORDER BY id LIMIT 1",
            (quote.session_id, row["quote_message_id"], quote.rfq.message_id),
        ).fetchone()
        if mail:
            _sent_quote(ctx, quote, Message.model_validate_json(mail[0]))
        elif not ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='mail.held' "
            "AND json_extract(payload,'$.reason')='dry_run' "
            "AND json_extract(payload,'$.purpose') IN ('quote','fallback_quote')",
            (quote.session_id,),
        ).fetchone():
            ctx.db.execute(
                "INSERT INTO mail_delivery VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    row["key"],
                    quote.session_id,
                    "fallback_quote" if quote.fallback else "quote",
                    quote.rfq.customer,
                    quote.subject,
                    quote.body,
                    quote.rfq.message_id,
                    quote.id,
                ),
            )
    for row in ctx.db.execute("SELECT operation, session_id, purpose FROM mail_delivery WHERE sent_id IS NULL"):
        emit(
            ctx,
            "error",
            "outbox",
            session_id=row["session_id"],
            payload={"where": "delivery_unconfirmed", "purpose": row["purpose"]},
        )


async def reconcile_reply(ctx: Context, incoming: Message) -> Message:
    """A reply names its parents. Fetch a missing sent parent only when a pending send to its sender exists.
    The API exposes individual sent mail by id but does not provide a sent-folder listing or idempotency keys."""
    refs = dict.fromkeys([incoming.in_reply_to, *reversed(incoming.references)])
    if not any(refs):
        return incoming
    pending = ctx.db.execute(
        "SELECT * FROM mail_delivery WHERE sent_id IS NULL AND recipient=?", (incoming.sender,)
    ).fetchall()
    if not pending:
        return incoming
    for ref in refs:
        if ref is None or ctx.db.execute("SELECT 1 FROM messages WHERE id=?", (ref,)).fetchone():
            continue
        try:
            mail = await ctx.mailbox.get(ref)
        except Exception as exc:  # noqa: BLE001  keep the original claim for the next piece of evidence
            emit(
                ctx,
                "error",
                "outbox",
                message_id=incoming.id,
                session_id=incoming.session_id,
                payload={"where": "delivery_lookup", "error": type(exc).__name__},
            )
            continue
        for row in pending:
            if (mail.sender.casefold(), mail.to.casefold(), mail.subject, mail.body, mail.in_reply_to) != (
                incoming.to.casefold(),
                row["recipient"],
                row["subject"],
                row["body"],
                row["parent_id"],
            ):
                continue
            sent = _receipt(ctx, row["operation"], mail)
            incoming = incoming.model_copy(update={"session_id": sent.session_id})
            ctx.db.execute(
                "UPDATE messages SET session_id=?, data=? WHERE id=?",
                (sent.session_id, incoming.model_dump_json(), incoming.id),
            )
            return incoming
    return incoming
