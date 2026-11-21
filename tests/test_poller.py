import asyncio
import contextlib
import dataclasses
import json
from pathlib import Path

from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.core import poller
from pixel.core.context import Context

MAILS = Path(__file__).parent / "fixtures" / "mails"


class FakeMailbox:
    """Answers like the API: everything with id > since_id."""

    def __init__(self, names):
        self.mails = [to_message(json.loads((MAILS / n).read_text()), "in") for n in names]
        self.since = []

    async def poll(self, since_id, limit=200):
        self.since.append(since_id)
        found = [m for m in self.mails if m.id > since_id]
        return found, max((m.id for m in found), default=since_id)


async def run_briefly(ctx, seen):
    async def on_message(message):
        seen.append(message.id)

    task = asyncio.create_task(poller.run(ctx, on_message))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_each_mail_once_across_a_restart(conn):
    config = dataclasses.replace(settings(), poll_seconds=0.01)
    mailbox = FakeMailbox(["synthetic-rfq.json", "synthetic-tracking.json"])
    seen = []
    await run_briefly(Context(config, conn, mailbox, None, None, None), seen)
    restarted = FakeMailbox(["synthetic-rfq.json", "synthetic-tracking.json"])
    await run_briefly(Context(config, conn, restarted, None, None, None), seen)
    assert seen == [1001, 1007]
    assert restarted.since[0] == 1007
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'mail.in'").fetchone()[0] == 2
