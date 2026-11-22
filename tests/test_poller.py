import asyncio
import contextlib
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import Booking
from pixel.core import poller
from pixel.core.context import Context
from pixel.crm import bookings

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
    mailbox = FakeMailbox(["rfq-door-no-origin.json", "synthetic-tracking.json"])
    seen = []
    await run_briefly(Context(config, conn, mailbox, None, None, None), seen)
    restarted = FakeMailbox(["rfq-door-no-origin.json", "synthetic-tracking.json"])
    await run_briefly(Context(config, conn, restarted, None, None, None), seen)
    assert seen == [m.id for m in mailbox.mails]
    assert restarted.since[0] == max(seen)
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'mail.in'").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("subject", "body", "known"),
    [
        ("Booking BK-123456 confirmed", "", True),
        ("Confirmation", "Booking bk-123456, is confirmed.", True),
        ("Booking BK-unknown confirmed", "", False),
    ],
)
def test_standalone_confirmation_session(conn, subject, body, known):
    template = FakeMailbox(["synthetic-tracking.json"]).mails[0]
    message = template.model_copy(
        update={
            "subject": subject,
            "body": body,
            "in_reply_to": None,
            "references": (),
        }
    )
    ctx = Context(settings(), conn, None, None, None, None)
    booking = Booking(
        id="booking",
        quote_id="quote",
        session_id=message.id + 1,
        team_reference="team",
        via="portal",
        carrier="carrier",
        booking_reference="BK-123456",
        created_at=message.sent_at,
    )
    bookings.save(ctx, booking)
    expected = booking.session_id if known else message.id
    assert poller.ingest(ctx, message).session_id == expected
    assert conn.execute("SELECT session_id FROM events WHERE kind = 'mail.in'").fetchone()[0] == expected


def _rfq_at(stored_at):
    raw = json.loads((MAILS / "rfq-door-no-origin.json").read_text())
    return to_message({**raw, "received_at": stored_at.isoformat()}, "in")


@pytest.mark.parametrize(
    ("offset", "evidence", "used"),
    [
        (timedelta(hours=1), None, timedelta(0)),  # a naive CET time read as UTC: now, never an hour ahead
        (timedelta(seconds=3), None, timedelta(seconds=3)),  # within the slack: kept
        (timedelta(hours=-1), None, timedelta(hours=-1)),  # no evidence (a backlog after a restart): kept, expired
        (timedelta(hours=-1), timedelta(seconds=-20), timedelta(seconds=-20)),  # stored after a complete poll
        (timedelta(minutes=-4), timedelta(seconds=-20), timedelta(minutes=-4)),  # time left: the stamp stands
    ],
)
def test_a_stored_time_is_moved_only_when_it_is_impossible(conn, monkeypatch, offset, evidence, used):
    now = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)
    monkeypatch.setattr(poller.timing, "now", lambda: now)
    ctx = Context(settings(), conn, None, None, None, None)
    stored = poller.ingest(ctx, _rfq_at(now + offset), now + evidence if evidence is not None else None)
    assert stored.sent_at == now + used
    moved = conn.execute("SELECT count(*) FROM events WHERE kind = 'mail.clock'").fetchone()[0]
    assert moved == (used != offset)


async def test_a_request_stamped_an_hour_early_still_gets_its_quote(conn, monkeypatch):
    """The second poll proves the mail was stored after the first one started: answered, not dropped as expired."""
    from pixel.core import orchestrator

    config = dataclasses.replace(settings(), poll_seconds=0.001)
    mailbox = FakeMailbox([])
    ctx = Context(config, conn, mailbox, None, None, None)
    late = _rfq_at(datetime.now(UTC) - timedelta(hours=1))
    real_poll = mailbox.poll

    async def poll(since_id, limit=200):
        if len(mailbox.since) == 2:
            mailbox.mails = [late]
        return await real_poll(since_id, limit)

    mailbox.poll = poll
    seen = []
    await run_briefly(ctx, seen)
    assert seen == [late.id]
    row = conn.execute("SELECT sent_at FROM messages WHERE id = ?", (late.id,)).fetchone()
    stored_at = datetime.fromisoformat(row["sent_at"])
    deadline = orchestrator.deadline_for(stored_at, config)
    assert deadline.expires_at > datetime.now(UTC) + timedelta(minutes=5)
