"""The only way mail leaves. Enforces first-quote-only, the recipient list, the mail limit, dry run and takeover."""

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta

from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.contracts import Message, Quote, QuoteStatus
from pixel.core import db, pdf, timing
from pixel.core.context import Context
from pixel.core.events import emit, scrub
from pixel.crm import quotes

MAILS_PER_MINUTE = 60  # R7
HUMAN = "human"  # purpose of mail a person sends from the UI: passes a takeover
# A quote or no-price reply the mailbox refused with a 5xx, or whose POST never got an answer, is sent again for this
# long: a 503 lasting seconds took the agent's send and the fallback's alike (R1). The caller's deadline cuts it short.
RESEND_S = 60.0
RESEND_BACKOFF_S = (0.5, 1.0, 2.0, 4.0)
RESENT = ("no_price",)  # besides quotes: the one answer a request without a price gets
UNCONNECTED = {"ConnectError", "ConnectTimeout", "PoolTimeout"}  # failed before the server: no mail
SETTLED = "delivery-settled:"  # a failed send its `error` event reported: no `delivery_unconfirmed` at a restart


class NotSent(Exception):
    """Refused before the API was called: `reason` is dry_run, human, duplicate_quote, superseded or recipient."""

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
    """True once a quote for the session was sent, held by dry run, or is being sent; also while the customer's
    change is priced again (`agents/rfq/agent.py` `requote`), which then answers instead."""
    key = f"quote:{session_id}"
    return bool(
        db.get(ctx.db, key)
        or db.get(ctx.db, f"requote:{session_id}")
        or ctx.db.execute(
            "SELECT 1 FROM mail_delivery WHERE operation = ? OR operation LIKE ?", (key, f"{key}:%")
        ).fetchone()
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


def _hold(
    ctx: Context, to: str, sid: int | None, purpose: str, quote: Quote | None, operator: bool, parent: int | None
) -> str | None:
    if purpose == "no_price" and sid is not None and quoted(ctx, sid):
        return "duplicate_quote"
    if not allowed(ctx, to):
        return "recipient"
    if (
        quote
        and parent == quote.rfq.message_id
        and ctx.db.execute(
            "SELECT 1 FROM mail_delivery WHERE session_id=? AND parent_id=? AND purpose='no_price'",
            (sid, parent),
        ).fetchone()
    ):
        return "duplicate_quote"
    # The customer changed the request before our quote: the answer to the change mail goes out, never the older facts.
    if quote and parent == quote.rfq.message_id and db.get(ctx.db, f"requote:{sid}"):
        return "superseded"
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


def _reference(ctx: Context, quote: Quote) -> str | None:
    row = ctx.db.execute("SELECT subject, body FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
    return pdf.reference(f"{row['subject']}\n{row['body']}") if row else None


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
    english: str | None = None,
) -> Message:
    to = to.strip().lower()
    # Every quote carries itself as a PDF: a customer who asked for one rejects the quote without it (round 76).
    attachments = (
        ((f"offer-{quote.id}.pdf", "application/pdf", pdf.offer(quote, _reference(ctx, quote))),) if quote else ()
    )

    def operation(text: str) -> str:
        # Reply to a particular tracking question exactly once, even if its CRM data changes during a restart.
        key = json.dumps(
            [sid, purpose, to, parent, "" if purpose in ("tracking", "no_price") else text], ensure_ascii=False
        )
        if quote:
            # A requote answers the customer's change mail: its own claim, so the first quote's never blocks it.
            return f"quote:{sid}" if parent == quote.rfq.message_id else f"quote:{sid}:{parent}"
        return uuid.uuid4().hex if purpose == HUMAN else hashlib.sha256(key.encode()).hexdigest()

    key = operation(body)
    payload = {"to": to, "subject": subject, "body": body, "in_reply_to": parent, "purpose": purpose}
    resend_until, resends, retried = asyncio.get_running_loop().time() + RESEND_S, 0, False
    while True:
        if sent := _claimed(ctx, key, quote is not None):
            return sent
        reason = _hold(ctx, to, sid, purpose, quote, operator, parent)
        if reason:
            emit(ctx, "mail.held", "outbox", session_id=sid, payload={**payload, "reason": reason})
            raise NotSent(reason)
        await _wait_for_slot(ctx)
        # While waiting another sender may have won the quote or a person may have taken over.
        if sent := _claimed(ctx, key, quote is not None):
            return sent
        if reason := _hold(ctx, to, sid, purpose, quote, operator, parent):
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
            sent = await ctx.mailbox.send(to, subject, body, parent, attachments)
        except ApiError as e:
            # Rejection, or failure before connecting, definitely produced no mail. A lost POST response is ambiguous,
            # except for a quote: only the first quote counts (R4), so a second one is harmless, a lost one is not.
            if quote is not None or 400 <= e.status < 500 or e.status == 0 and e.detail in UNCONNECTED:
                ctx.db.execute("DELETE FROM mail_delivery WHERE operation = ?", (key,))
            if e.status == 429:
                await asyncio.sleep(e.retry_after or 1.0)
                continue
            if e.status == 503 and quote is None:
                ctx.db.execute("DELETE FROM mail_delivery WHERE operation = ?", (key,))  # a 503 is an answer: not sent
            pause = RESEND_BACKOFF_S[min(resends, len(RESEND_BACKOFF_S) - 1)]
            resend = quote is not None or (purpose in RESENT and e.status == 503)
            if (
                resend
                and (e.status >= 500 or e.status == 0)
                and asyncio.get_running_loop().time() + pause < resend_until
            ):
                resends += 1
                emit(ctx, "mail.resend", "outbox", session_id=sid,
                     payload={"purpose": purpose, "status": e.status, "try": resends + 1})  # fmt: skip
                await asyncio.sleep(pause)
                continue
            if attachments and 400 <= e.status < 500:  # refused with its PDF: the quote matters more (R1)
                emit(ctx, "error", "outbox", session_id=sid,
                     payload={"where": "attachment", "error": scrub(ctx, str(e))})  # fmt: skip
                attachments = ()
                continue
            translated = english is not None and english != body and 400 <= e.status < 500
            if quote is None:
                # Not sent for sure (a 4xx, a 503, no connection: its row is gone above): once more, never a loop; a
                # refused translation in English. A lost response or another 5xx may have been delivered: its row stays
                # as the guard against a second mail, settled so that no restart reports it again (C15, session 51345).
                refused = 400 <= e.status < 500 or e.status == 503 or e.status == 0 and e.detail in UNCONNECTED
                if not refused:
                    db.put(ctx.db, SETTLED + key, "failed")
                elif not retried and (translated or not 400 <= e.status < 500):
                    retried = True
                    emit(ctx, "mail.resend", "outbox", session_id=sid,
                         payload={"purpose": purpose, "status": e.status, "try": 2, "english": translated})  # fmt: skip
                    if translated:
                        body, key = english, operation(english)
                        payload["body"] = body
                    await asyncio.sleep(RESEND_BACKOFF_S[0])
                    continue
            emit(
                ctx,
                "error",
                "outbox",
                session_id=sid,
                payload={**payload, "where": "send", "error": scrub(ctx, str(e)), "retried": retried},
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
    english: str | None = None,
) -> Message:
    """Send a non-quote. A replay of a delivered automatic action reuses its receipt. `english`: the text `body`
    translates, sent instead when the mailbox refuses the translation."""
    return await _deliver(ctx, to, subject, body, session_id, in_reply_to, purpose, english=english)


async def send_quote(ctx: Context, quote: Quote, *, operator: bool = False, reply_to: int | None = None) -> Quote:
    """Claim the session immediately before calling the mailbox and persist the returned receipt. `reply_to`: the
    customer's mail that changed the cargo after our quote; the scorer scores our answer to it (round 86)."""
    row = ctx.db.execute("SELECT sender FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
    to = row["sender"] if row else quote.rfq.customer
    await _deliver(
        ctx,
        to,
        quote.subject,
        quote.body,
        quote.session_id,
        reply_to or quote.rfq.message_id,
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
        if db.get(ctx.db, SETTLED + row["operation"]):
            continue
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
