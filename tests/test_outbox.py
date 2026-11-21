import asyncio
import dataclasses
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest

from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK, to_message
from pixel.config import settings
from pixel.contracts import Quote, RateOption, Rfq, Source
from pixel.core import outbox, poller
from pixel.core.context import Context
from pixel.core.outbox import NotSent

MAILS = Path(__file__).parent / "fixtures" / "mails"
KEY = "marker-key-0123456789"


class FakeMailbox:
    def __init__(self, fail_with=()):
        self.sent = []
        self.attachments = []
        self.fail_with = list(fail_with)

    async def send(self, to, subject, body, in_reply_to=None, attachments=()):
        await asyncio.sleep(0)
        if self.fail_with:
            raise self.fail_with.pop(0)
        self.sent.append(to)
        self.attachments.append(attachments)
        return to_message(
            {"id": 5000 + len(self.sent), "from": "us@x", "to": to, "subject": subject, "body": body,
             "in_reply_to": in_reply_to, "created_at": datetime.now(UTC).isoformat()}, "out",
        )  # fmt: skip


def make_ctx(conn, mailbox=None, **changes):
    config = dataclasses.replace(settings(), **{"dry_run": False, "api_key": KEY, **changes})
    return Context(config, conn, mailbox or FakeMailbox(), None, None, None)


def ingest_rfq(ctx, fixture="rfq-door-no-origin.json"):
    rfq = poller.ingest(ctx, to_message(json.loads((MAILS / fixture).read_text()), "in"))
    ctx.db.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (?, ?, 'open', ?)",
                   (rfq.id, rfq.sender, rfq.sent_at.isoformat()))  # fmt: skip
    return rfq


def make_quote(rfq, quote_id="Q-1", fallback=False):
    total = Decimal("100.00")
    option = RateOption(
        source=Source.SHEET,
        carrier="c",
        origin="AAA",
        destination="BBB",
        chargeable_kg=total,
        billed_kg=total,
        freight=total,
        total=total,
        currency="EUR",
        total_eur=total,
        transit_days=1,
    )
    request = Rfq(
        session_id=rfq.session_id,
        message_id=rfq.id,
        customer=rfq.sender,
        origin="AAA",
        destination="BBB",
        weight_kg=total,
        received_at=rfq.sent_at,
        deadline_at=rfq.sent_at,
    )
    return Quote(id=quote_id, session_id=rfq.session_id, rfq=request, option=option, total=total, currency="EUR",
                 subject="Re: quote", body=f"{KEY} must not leak", created_at=rfq.sent_at,
                 fallback=fallback)  # fmt: skip


def kinds(ctx):
    return [r[0] for r in ctx.db.execute("SELECT kind FROM events ORDER BY id")]


async def test_two_racing_quotes_send_one(conn):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    results = await asyncio.gather(
        outbox.send_quote(ctx, make_quote(rfq, "Q-1")), outbox.send_quote(ctx, make_quote(rfq, "Q-2")),
        return_exceptions=True,
    )  # fmt: skip
    assert len(ctx.mailbox.sent) == 1
    assert [r.reason for r in results if isinstance(r, NotSent)] == ["duplicate_quote"]
    with pytest.raises(NotSent, match="duplicate_quote"):
        await outbox.send_quote(ctx, make_quote(rfq, "Q-3"))
    assert conn.execute("SELECT state FROM sessions").fetchone()[0] == "quoted"


async def test_the_quote_goes_out_with_itself_as_a_pdf(conn):
    """Round 76: the customer asked for the offer as a PDF and rejected a quote without one (`no_pdf_offer`)."""
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx, "rfq-door-offer-as-pdf.json")
    quote = make_quote(rfq).model_copy(update={"body": "Hallo Deniz,\n\n100.00 EUR all-in (Grüße).\n\nLena Berger"})
    await outbox.send_quote(ctx, quote)
    [(name, kind, content)] = ctx.mailbox.attachments[0]
    assert (name, kind) == ("offer-Q-1.pdf", "application/pdf")
    with pdfplumber.open(io.BytesIO(content)) as document:
        assert "100.00 EUR all-in (Grüße)." in document.pages[0].extract_text()


async def test_a_refused_pdf_never_costs_the_quote(conn):
    ctx = make_ctx(conn, FakeMailbox([ApiError(422, "attachment")]))
    rfq = ingest_rfq(ctx)
    await outbox.send_quote(ctx, make_quote(rfq))
    assert ctx.mailbox.attachments == [()] and "error" in kinds(ctx)


async def test_dry_run_holds_and_keeps_the_quote_slot(conn):
    ctx = make_ctx(conn, dry_run=True)
    rfq = ingest_rfq(ctx)
    with pytest.raises(NotSent, match="dry_run"):
        await outbox.send_quote(ctx, make_quote(rfq))
    assert ctx.mailbox.sent == []
    assert "mail.held" in kinds(ctx)
    assert outbox.quoted(ctx, rfq.session_id)


async def test_429_is_waited_out_not_dropped(conn):
    ctx = make_ctx(conn, FakeMailbox([ApiError(429, "slow down", retry_after=0.01)]))
    rfq = ingest_rfq(ctx)
    await outbox.send_quote(ctx, make_quote(rfq))
    assert len(ctx.mailbox.sent) == 1


async def test_a_failed_send_frees_the_slot_for_the_fallback(conn):
    ctx = make_ctx(conn, FakeMailbox([ApiError(422, "bad")] * 2))
    rfq = ingest_rfq(ctx)
    with pytest.raises(ApiError):
        await outbox.send_quote(ctx, make_quote(rfq))
    await outbox.send_quote(ctx, make_quote(rfq, fallback=True))
    assert len(ctx.mailbox.sent) == 1


async def test_unknown_recipient_is_refused(conn):
    ctx = make_ctx(conn)
    with pytest.raises(NotSent, match="recipient"):
        await outbox.send(ctx, to="someone@else.example", subject="s", body="b", session_id=None, purpose="x")
    assert ctx.mailbox.sent == []


async def test_takeover_holds_automatic_mail_but_not_the_fallback(conn):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    conn.execute("UPDATE sessions SET mode = 'human'")
    with pytest.raises(NotSent, match="human"):
        await outbox.send_quote(ctx, make_quote(rfq))
    await outbox.send_quote(ctx, make_quote(rfq, fallback=True))
    assert len(ctx.mailbox.sent) == 1


@pytest.mark.parametrize("purpose", ["bid_request", "bid_pickup", "ratedesk"])
async def test_takeover_allows_sourcing_mail(conn, purpose):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    conn.execute("UPDATE sessions SET mode = 'human'")
    carrier = "carrier@example.test"
    conn.execute("INSERT INTO carriers (name, kind, email, serves) VALUES ('c', 'airline', ?, '')", (carrier,))
    recipient = RATEDESK if purpose == "ratedesk" else carrier

    await outbox.send(ctx, to=recipient, subject="s", body="b", session_id=rfq.session_id, purpose=purpose)
    with pytest.raises(NotSent, match="human"):
        await outbox.send(ctx, to=rfq.sender, subject="s", body="b", session_id=rfq.session_id, purpose="tracking")
    assert ctx.mailbox.sent == [recipient]


async def test_no_key_in_any_event(conn):
    ctx = make_ctx(conn, FakeMailbox([ApiError(422, f"echo {KEY}")] * 2))
    rfq = ingest_rfq(ctx)
    with pytest.raises(ApiError):
        await outbox.send_quote(ctx, make_quote(rfq))
    await outbox.send_quote(ctx, make_quote(rfq))
    payloads = " ".join(r[0] for r in conn.execute("SELECT payload FROM events"))
    assert "must not leak" in payloads
    assert KEY not in payloads
