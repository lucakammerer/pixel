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
from pixel.core import db, outbox, poller
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


async def test_while_a_change_is_priced_again_the_older_quote_is_held_and_the_answer_to_the_change_leaves(conn):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    db.put(conn, f"requote:{rfq.session_id}", "77")
    assert outbox.quoted(ctx, rfq.session_id)
    with pytest.raises(NotSent, match="superseded"):
        await outbox.send_quote(ctx, make_quote(rfq, "Q-1"))
    change = poller.ingest(ctx, to_message({"id": 77, "from": rfq.sender, "to": "us@x", "subject": "Re: x",
                                            "body": "320 kg now", "in_reply_to": rfq.id, "references": [rfq.id],
                                            "created_at": datetime.now(UTC).isoformat()}, "in"))  # fmt: skip
    await outbox.send_quote(ctx, make_quote(rfq, "Q-2"), reply_to=change.id)
    conn.execute("DELETE FROM kv WHERE key = ?", (f"requote:{rfq.session_id}",))
    assert len(ctx.mailbox.sent) == 1 and outbox.quoted(ctx, rfq.session_id)


async def test_the_quote_goes_out_with_itself_as_a_pdf(conn):
    """Round 76: the customer asked for the offer as a PDF and rejected a quote without one (`no_pdf_offer`)."""
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx, "rfq-door-offer-as-pdf.json")
    quote = make_quote(rfq).model_copy(update={"body": "Hallo Deniz,\n\n100.00 EUR all-in (Grüße).\n\nLena Berger"})
    await outbox.send_quote(ctx, quote)
    [(name, kind, content)] = ctx.mailbox.attachments[0]
    assert (name, kind) == ("offer-Q-1.pdf", "application/pdf")
    # Round 78: a file ending in %EOF instead of %%EOF reads as truncated to a strict reader; the price was lost.
    assert content.endswith(b"\n%%EOF\n")
    with pdfplumber.open(io.BytesIO(content)) as document:
        assert "100.00 EUR all-in (Grüße)." in document.pages[0].extract_text()


async def test_the_pdf_echoes_their_reference_and_breaks_down_only_a_total_that_adds_up(conn):
    ctx = make_ctx(conn)
    rfq = ingest_rfq(ctx)
    conn.execute("UPDATE messages SET body = body || ? WHERE id = ?", ("\nOur ref: RE-4711", rfq.id))
    quote = make_quote(rfq)
    option = quote.option.model_copy(update={"freight": Decimal(80), "fuel": Decimal(15), "security": Decimal(5),
                                             "billed_kg": Decimal(300)})  # fmt: skip
    await outbox.send_quote(ctx, quote.model_copy(update={"option": option}))
    with pdfplumber.open(io.BytesIO(ctx.mailbox.attachments[0][0][2])) as document:
        text = document.pages[0].extract_text()
    assert "Your reference: RE-4711" in text and "billed on 300 kg" in text and "Fuel surcharge: 15.00 EUR" in text
    off = quote.model_copy(update={"option": option.model_copy(update={"fuel": Decimal(16)})})
    assert not any("surcharge" in line for line in outbox.pdf.details(off))


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


async def test_a_mailbox_5xx_on_the_quote_is_sent_again_not_lost(conn, monkeypatch):
    monkeypatch.setattr(outbox, "RESEND_BACKOFF_S", (0.0,))
    failures = [ApiError(503, "mailbox busy"), ApiError(502, "bad gateway"), ApiError(0, "ReadTimeout")]
    ctx = make_ctx(conn, FakeMailbox(failures))
    rfq = ingest_rfq(ctx)
    await outbox.send_quote(ctx, make_quote(rfq))
    assert ctx.mailbox.sent == [rfq.sender]
    assert outbox.quoted(ctx, rfq.session_id)
    assert kinds(ctx).count("mail.resend") == 3


async def test_resending_stops_when_its_time_is_up(conn, monkeypatch):
    monkeypatch.setattr(outbox, "RESEND_BACKOFF_S", (0.0,))
    monkeypatch.setattr(outbox, "RESEND_S", 0.0)
    ctx = make_ctx(conn, FakeMailbox([ApiError(503, "mailbox busy")] * 5))
    rfq = ingest_rfq(ctx)
    with pytest.raises(ApiError):
        await outbox.send_quote(ctx, make_quote(rfq))
    assert not outbox.quoted(ctx, rfq.session_id)  # the claim is free for the fallback
    assert kinds(ctx).count("mail.resend") == 0


class RecordingMailbox(FakeMailbox):
    async def send(self, to, subject, body, in_reply_to=None, attachments=()):
        self.bodies = [*getattr(self, "bodies", []), body]
        return await super().send(to, subject, body, in_reply_to, attachments)


def unconfirmed(ctx):
    outbox.repair_receipts(ctx)
    return [
        r for r in ctx.db.execute("SELECT payload FROM events WHERE kind = 'error'") if "delivery_unconfirmed" in r[0]
    ]


@pytest.mark.parametrize("purpose", ["booking", "tracking", "question", "bid_request", "bid_pickup", "ratedesk"])
@pytest.mark.parametrize(
    "answers",
    [
        [ApiError(500, "Internal Server Error")],
        [ApiError(503, "busy")],
        [ApiError(500, "Internal Server Error"), ApiError(429, "slow down", retry_after=0.01)],
        [ApiError(500, "Internal Server Error")] * 2,
    ],
)
async def test_a_non_quote_mail_the_mailbox_refuses_with_a_5xx_is_tried_once_more_then_settled(
    conn, monkeypatch, purpose, answers
):
    """C15, session 51345: the booking notice got a 500 and was never sent again; its entry was reported at every
    restart. One more try, then settled either way; a 429 between is waited out, not counted."""
    monkeypatch.setattr(outbox, "RESEND_BACKOFF_S", (0.0,))
    monkeypatch.setattr(outbox, "RESENT", ())  # `no_price`'s own resending aside
    failures = sum(e.status >= 500 for e in answers)
    ctx = make_ctx(conn, RecordingMailbox(answers))
    rfq = ingest_rfq(ctx)
    to = {"ratedesk": RATEDESK}.get(purpose, rfq.sender)
    if purpose in ("bid_request", "bid_pickup"):
        to = "carrier@example.test"
        kind = "airline" if purpose == "bid_request" else "trucker"
        conn.execute("INSERT INTO carriers (name, kind, email, serves) VALUES ('c', ?, ?, '')", (kind, to))
    send = outbox.send(ctx, to=to, subject="s", body="b", session_id=rfq.session_id, purpose=purpose)
    if failures == 1:
        await send
        assert ctx.mailbox.sent == [to]
    else:
        with pytest.raises(ApiError):
            await send
        assert ctx.mailbox.sent == []
        assert "error" in kinds(ctx)
    assert unconfirmed(ctx) == []  # nothing reported again at a restart


async def test_a_refused_translation_is_tried_once_more_in_english(conn, monkeypatch):
    monkeypatch.setattr(outbox, "RESEND_BACKOFF_S", (0.0,))
    ctx = make_ctx(conn, RecordingMailbox([ApiError(500, "Internal Server Error")]))
    rfq = ingest_rfq(ctx)
    await outbox.send(ctx, to=rfq.sender, subject="s", body="Say\x013n Deniz", session_id=rfq.session_id,
                      purpose="booking", english="Dear Deniz")  # fmt: skip
    assert ctx.mailbox.bodies == ["Say\x013n Deniz", "Dear Deniz"]
    assert unconfirmed(ctx) == []


async def test_a_non_quote_mail_refused_with_a_4xx_is_not_tried_again_but_settled(conn):
    ctx = make_ctx(conn, RecordingMailbox([ApiError(422, "bad")]))
    rfq = ingest_rfq(ctx)
    with pytest.raises(ApiError):
        await outbox.send(ctx, to=rfq.sender, subject="s", body="b", session_id=rfq.session_id, purpose="booking")
    assert ctx.mailbox.bodies == ["b"]
    assert unconfirmed(ctx) == []
