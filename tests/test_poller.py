import asyncio
import contextlib
import dataclasses
import json
from pathlib import Path

import pytest

from pixel.api.mailbox import RATEDESK, to_message
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


def test_desk_matches_unique_subject_and_leaves_ambiguous_legacy_subject_unassigned(conn):
    ctx = Context(settings(), conn, None, None, None, None)
    template = FakeMailbox(["synthetic-tracking.json"]).mails[0]
    for mid, sid, subject in [(1, 10, "Rate request"), (2, 20, "Rate request"), (3, 10, "Rate request [RFQ 10]")]:
        poller.store(ctx, template.model_copy(update={"id": mid, "session_id": sid, "direction": "out",
                                                      "to": RATEDESK, "subject": subject}))  # fmt: skip
    reply = template.model_copy(update={"sender": RATEDESK, "in_reply_to": None, "references": ()})
    ambiguous = reply.model_copy(update={"body": 'Rates for your mail "Rate request"'})
    specific = reply.model_copy(update={"body": 'Rates for your mail "Rate request [RFQ 10]"'})
    assert poller.session_of(ctx, ambiguous) == reply.id
    assert poller.session_of(ctx, specific) == 10
    assert poller.session_of(ctx, ambiguous.model_copy(update={"in_reply_to": 2})) == 20
