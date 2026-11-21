"""Polls the mailbox, stores each mail once, hands it to the orchestrator."""

import asyncio
from collections.abc import Awaitable, Callable

from pixel.api.mailbox import mapping_gaps
from pixel.contracts import Message
from pixel.core import db
from pixel.core.context import Context
from pixel.core.events import emit

SINCE_KEY = "poller.since_id"


def session_of(ctx: Context, message: Message) -> int:
    """The customer thread a mail belongs to: via `references`, or via the mail of ours it answers
    (a carrier bid answers a bid request we sent for a session)."""
    for ref in (message.in_reply_to, *reversed(message.references)):
        if ref is None:
            continue
        row = ctx.db.execute("SELECT session_id FROM messages WHERE id = ?", (ref,)).fetchone()
        if row:
            return row["session_id"]
    return message.references[0] if message.references else message.id


def store(ctx: Context, message: Message) -> bool:
    """Stores a mail with its session; False when it is already stored."""
    cursor = ctx.db.execute(
        "INSERT OR IGNORE INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
        " in_reply_to, sent_at, data) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        (message.id, message.session_id, message.direction, message.kind, message.sender, message.to,
         message.subject, message.body, message.in_reply_to, message.sent_at.isoformat(), message.model_dump_json()),
    )  # fmt: skip
    return cursor.rowcount == 1


def ingest(ctx: Context, message: Message) -> Message | None:
    """Session, store, `mail.in`. None for a mail we already hold."""
    message = message.model_copy(update={"session_id": session_of(ctx, message)})
    if not store(ctx, message):
        return None
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


async def run(ctx: Context, on_message: Callable[[Message], Awaitable[None]]) -> None:
    """Loops forever: poll with the stored `since_id`, store, emit `mail.in`, dispatch without blocking the loop.
    `since_id` moves only after a mail is stored, so a restart neither loses nor repeats one (R2)."""
    since_id = int(db.get(ctx.db, SINCE_KEY) or 0)
    failing = False
    while True:
        try:
            messages, next_since_id = await ctx.mailbox.poll(since_id)
            failing = False
        except Exception as e:  # noqa: BLE001
            if not failing:  # one event per outage, not one per second
                emit(ctx, "error", "poller", payload={"where": "poll", "error": f"{type(e).__name__}: {e}"})
            failing = True
            await asyncio.sleep(ctx.settings.poll_seconds)
            continue
        for message in messages:
            try:
                stored = ingest(ctx, message)
            except Exception as e:  # noqa: BLE001  one bad mail must not stop the mailbox
                emit(ctx, "error", "poller", message_id=message.id, payload={"where": "ingest", "error": repr(e)})
                stored = None
            db.put(ctx.db, SINCE_KEY, str(message.id))
            if stored:
                spawn(ctx, on_message(stored))
        if next_since_id > since_id:
            since_id = next_since_id
            db.put(ctx.db, SINCE_KEY, str(since_id))
        if len(messages) < 200:
            await asyncio.sleep(ctx.settings.poll_seconds)
