"""Polls the mailbox, stores each mail once, hands it to the orchestrator. `core/webhook.py` feeds `accept` too."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from math import isfinite

from pixel.agents.rfq.booking import CONFIRMED
from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, mapping_gaps
from pixel.contracts import Message
from pixel.core import db, timing
from pixel.core.context import Context
from pixel.core.events import emit
from pixel.crm import bookings

SINCE_KEY = "poller.since_id"
NAMES_OUR_MAIL = re.compile(r'your mail "(.+?)"')
POLL_LIMIT = re.compile(r"\bone poll per (\d+(?:\.\d+)?) seconds?\b", re.IGNORECASE)
POLL_PAGE = 200
# How far a stored time may lie past the evidence before it is corrected: clocks and the API's own lag.
AHEAD_SLACK = timedelta(seconds=5)
POLL_SLACK = timedelta(seconds=10)
INGEST_TRIES = 3  # a mail that cannot be stored after this many polls is skipped, so it cannot block every later one


def session_of(ctx: Context, message: Message) -> int:
    """The customer thread a mail belongs to: via `references`, via the mail of ours it answers (a carrier bid
    answers a bid request we sent for a session), or, for the rate desk, via the subject of our mail that its answer names."""
    for ref in (message.in_reply_to, *reversed(message.references)):
        if ref is None:
            continue
        row = ctx.db.execute("SELECT session_id FROM messages WHERE id = ?", (ref,)).fetchone()
        if row:
            return row["session_id"]
    if message.references:
        return message.references[0]
    # The rate desk answers in a thread of its own and names our mail by its subject.
    if message.sender == RATEDESK and (named := NAMES_OUR_MAIL.search(message.body)):
        row = ctx.db.execute(
            "SELECT session_id FROM messages WHERE direction = 'out' AND recipient = ? AND subject = ?"
            " ORDER BY id DESC LIMIT 1",
            (message.sender, named.group(1)),
        ).fetchone()
        if row:
            return row["session_id"]
    match = CONFIRMED.search(message.subject) or CONFIRMED.search(message.body)
    if match and (booking := bookings.by_reference(ctx, match.group(1).rstrip(".,:;"))):
        return booking.session_id
    return message.id


def store(ctx: Context, message: Message) -> bool:
    """Stores a mail with its session; False when it is already stored."""
    cursor = ctx.db.execute(
        "INSERT OR IGNORE INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
        " in_reply_to, sent_at, data) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        (message.id, message.session_id, message.direction, message.kind, message.sender, message.to,
         message.subject, message.body, message.in_reply_to, message.sent_at.isoformat(), message.model_dump_json()),
    )  # fmt: skip
    return cursor.rowcount == 1


def bounded(ctx: Context, message: Message, stored_after: datetime | None = None) -> Message:
    """The deadline runs from the mail's stored time (R1), so it is kept unless it is impossible: later than now, or
    already expired when we see it although `stored_after`, the evidence that the mail was stored after it (a signed
    push's time, a complete earlier poll without it), is later. A naive local time read as UTC puts a request an
    hour ahead and no timer fires before it expired; an hour behind, it is dropped as expired unanswered. A stored
    time that leaves the request any time is never moved: the evidence can be late too (a queued push). Each
    correction is a `mail.clock` event."""
    from pixel.core.orchestrator import EXPIRES_AFTER

    at, low, high = message.sent_at, stored_after, timing.now()
    expired = at <= high - EXPIRES_AFTER
    used = high if at > high + AHEAD_SLACK else low if expired and low is not None and at < low else at
    if used == at:
        return message
    emit(ctx, "mail.clock", "poller", session_id=message.session_id, message_id=message.id,
         payload={"stored_at": at.isoformat(), "used": used.isoformat(), "ahead": at > used})  # fmt: skip
    return message.model_copy(update={"sent_at": used})


def ingest(ctx: Context, message: Message, stored_after: datetime | None = None) -> Message | None:
    """Session, stored time within its evidence (`bounded`), store, `mail.in`. None for a mail we already hold."""
    message = message.model_copy(update={"session_id": session_of(ctx, message)})
    if ctx.db.execute("SELECT 1 FROM messages WHERE id = ?", (message.id,)).fetchone():
        return None
    message = bounded(ctx, message, stored_after)
    with db.atomic(ctx.db):
        if not store(ctx, message):
            return None
        ctx.db.execute("INSERT INTO mail_work(message_id) VALUES (?)", (message.id,))
        if gaps := mapping_gaps(message.raw):
            error = f"mail without {gaps}: check api/mailbox.py to_message"
            emit(ctx, "error", "poller", session_id=message.session_id, message_id=message.id,
                 payload={"where": "mapping", "error": error})  # fmt: skip
        emit(
            ctx,
            "mail.in",
            "poller",
            session_id=message.session_id,
            message_id=message.id,
            payload={
                "from": message.sender,
                "subject": message.subject,
                "in_reply_to": message.in_reply_to,
                "attachments": [a.filename for a in message.attachments],
                "sent_at": message.sent_at.isoformat(),
            },
        )
    return message


def spawn(ctx: Context, coro: Awaitable[None]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    ctx.tasks.add(task)
    task.add_done_callback(ctx.tasks.discard)
    return task


def accept(
    ctx: Context,
    message: Message,
    on_message: Callable[[Message], Awaitable[None]],
    stored_after: datetime | None = None,
) -> bool:
    """One mail from a poll or a push: ingest, then dispatch without blocking the caller."""
    try:
        stored = ingest(ctx, message, stored_after)
    except Exception as e:  # noqa: BLE001  one bad mail must not stop the mailbox
        emit(ctx, "error", "poller", message_id=message.id, payload={"where": "ingest", "error": repr(e)})
        return False
    if stored:
        spawn(ctx, on_message(stored))
    return True


async def run(ctx: Context, on_message: Callable[[Message], Awaitable[None]]) -> None:
    """Loops forever: poll with the stored `since_id`, store, emit `mail.in`, dispatch without blocking the loop.
    `since_id` moves only after a mail is stored, so a restart neither loses nor repeats one (R2)."""
    since_id = int(db.get(ctx.db, SINCE_KEY) or 0)
    interval = ctx.settings.poll_seconds
    failing = False
    complete_at = None  # when the last complete poll started: a mail it did not hold was stored after it
    tries: dict[int, int] = {}
    while True:
        started = timing.now()
        try:
            messages, next_since_id = await ctx.mailbox.poll(since_id, POLL_PAGE)
            failing = False
            for skipped in getattr(ctx.mailbox, "skipped", ()):
                emit(ctx, "error", "poller", payload={"where": "mapping", "error": f"mail {skipped['id']} skipped: "
                                                      f"{skipped['error']}, check api/mailbox.py to_message"})  # fmt: skip
        except Exception as e:  # noqa: BLE001
            if not failing:  # one event per outage, not one per second
                emit(ctx, "error", "poller", payload={"where": "poll", "error": f"{type(e).__name__}: {e}"})
            failing = True
            delay = interval
            if isinstance(e, ApiError) and e.status == 429:
                if limit := POLL_LIMIT.search(e.detail):
                    interval = max(interval, float(limit[1]))
                    delay = interval
                if e.retry_after is not None and isfinite(e.retry_after):
                    delay = max(delay, e.retry_after)
            await asyncio.sleep(delay)
            continue
        stored_after = complete_at - POLL_SLACK if complete_at else None
        complete_at = started if len(messages) < POLL_PAGE else None
        for message in messages:
            if not accept(ctx, message, on_message, stored_after):
                # Preserve the last durable cursor so a transient storage failure cannot lose this mail, but only a
                # few times: a mail that never stores would hold every later request behind it (R1).
                tries[message.id] = tries.get(message.id, 0) + 1
                if tries[message.id] < INGEST_TRIES:
                    break
                emit(ctx, "error", "poller", message_id=message.id,
                     payload={"where": "ingest", "error": f"skipped after {INGEST_TRIES} tries"})  # fmt: skip
            tries.pop(message.id, None)
            since_id = message.id
            db.put(ctx.db, SINCE_KEY, str(since_id))
        else:
            if next_since_id > since_id:
                since_id = next_since_id
                db.put(ctx.db, SINCE_KEY, str(since_id))
            if len(messages) >= POLL_PAGE and interval == ctx.settings.poll_seconds:
                continue
        await asyncio.sleep(interval)
