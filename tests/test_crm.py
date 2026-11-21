"""CRM tables and queries. The fakes here stand in for package A (event log, outbox) until it lands; the other
package C tests import them."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pixel.config import settings
from pixel.contracts import Event, Message, Quote, QuoteStatus, RateOption, Rfq, Source
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments

T0 = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


class Pipeline:
    """Fake `core.events` and `core.outbox`: events go into the real table, mail into `sent`."""

    def __init__(self, ctx: Context):
        self.ctx, self.sent, self.held = ctx, [], False

    def emit(self, ctx, kind, actor, *, session_id=None, message_id=None, payload=None, **metrics):
        cur = ctx.db.execute(
            "INSERT INTO events (ts, session_id, kind, actor, message_id, payload, tokens_in, tokens_out, cost_usd,"
            " duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                metrics.get("ts", datetime.now(UTC)).isoformat(), session_id, kind, actor, message_id,
                json.dumps(payload or {}), metrics.get("tokens_in", 0), metrics.get("tokens_out", 0),
                str(metrics.get("cost_usd", 0)), 0,
            ),
        )  # fmt: skip
        return Event(id=cur.lastrowid, ts=datetime.now(UTC), session_id=session_id, kind=kind, actor=actor,
                     message_id=message_id, payload=payload or {})  # fmt: skip

    def for_session(self, ctx, session_id, after_id=0):
        rows = ctx.db.execute(
            "SELECT id FROM events WHERE session_id = ? AND id > ? ORDER BY id", (session_id, after_id)
        )
        from pixel.ui.routes import _event

        return [_event(ctx, r["id"]) for r in rows.fetchall()]

    async def send(self, ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        if self.held:
            raise outbox.NotSent("human")
        self.sent.append({"to": to, "subject": subject, "body": body, "session_id": session_id,
                          "in_reply_to": in_reply_to, "purpose": purpose})  # fmt: skip
        return mail(900 + len(self.sent), session_id, sender="us@pixel", to=to, subject=subject, body=body)

    async def send_quote(self, ctx, quote):
        self.sent.append({"to": quote.rfq.customer, "subject": quote.subject, "body": quote.body, "purpose": "quote"})
        return quote.model_copy(
            update={"status": QuoteStatus.SENT, "sent_at": datetime.now(UTC), "sent_message_id": 999}
        )


@pytest.fixture
def pipe(conn, monkeypatch):
    ctx = Context(settings=settings(), db=conn, mailbox=None, portal=None, gateway=None, board=None)
    p = Pipeline(ctx)
    monkeypatch.setattr(events, "emit", p.emit)
    monkeypatch.setattr(events, "for_session", p.for_session)
    monkeypatch.setattr(outbox, "send", p.send)
    monkeypatch.setattr(outbox, "send_quote", p.send_quote)
    return p


def mail(id, session_id, *, sender="buyer@acme.example", to="us@pixel", subject="RFQ", body="", in_reply_to=None):
    return Message(id=id, session_id=session_id, direction="in", sender=sender, to=to, subject=subject, body=body,
                   in_reply_to=in_reply_to, sent_at=T0)  # fmt: skip


def quote(
    session_id=1, *, customer="buyer@acme.example", rate_id="R-abcdefgh12", status=QuoteStatus.SENT, total="370.74"
):
    rfq = Rfq(session_id=session_id, message_id=session_id, customer=customer, origin="FRA", destination="JFK",
              weight_kg=Decimal(150), received_at=T0, deadline_at=T0 + timedelta(minutes=6))  # fmt: skip
    option = RateOption(source=Source.PORTAL, carrier="Kestrel Airways", carrier_code="KS", origin="FRA",
                        destination="JFK", chargeable_kg=Decimal(150), billed_kg=Decimal(150), freight=Decimal(total),
                        total=Decimal(total), currency="EUR", total_eur=Decimal(total), transit_days=3, rate_id=rate_id)  # fmt: skip
    sent = T0 + timedelta(seconds=90) if status != QuoteStatus.DRAFT else None
    return Quote(id=f"Q-{session_id}", session_id=session_id, rfq=rfq, option=option, total=Decimal(total),
                 currency="EUR", subject="Your quote", body="Hello", status=status, created_at=T0, sent_at=sent,
                 sent_message_id=500 + session_id if sent else None)  # fmt: skip


def test_quote_found_by_session_and_status(pipe):
    ctx = pipe.ctx
    quotes.save(ctx, quote(1))
    quotes.set_status(ctx, "Q-1", QuoteStatus.ACCEPTED)
    assert quotes.for_session(ctx, 1).status == QuoteStatus.ACCEPTED
    assert quotes.get(ctx, "Q-1").total == Decimal("370.74")
    assert quotes.for_session(ctx, 2) is None


def test_shipment_found_by_every_reference(pipe):
    ctx = pipe.ctx
    s = shipments.create(ctx, quote(1))
    shipments.save(ctx, s.model_copy(update={"booking_reference": "BK-ABCD1234", "status": "booked"}))
    assert [x.id for x in shipments.find(ctx, reference=s.id.lower())] == [s.id]
    assert [x.id for x in shipments.find(ctx, reference="bk-abcd1234")] == [s.id]
    assert [x.id for x in shipments.find(ctx, customer="BUYER@acme.example", origin="fra", destination="JFK")] == [s.id]
    assert shipments.find(ctx, customer="other@x.example") == []
    assert shipments.find(ctx) == []


def test_booking_found_by_airline_reference(pipe):
    from pixel.contracts import Booking

    ctx = pipe.ctx
    bookings.save(ctx, Booking(id="B-1", quote_id="Q-1", session_id=1, team_reference="PX-1", via="portal",
                               carrier="Kestrel", booking_reference="BK-XYZ98765", created_at=T0))  # fmt: skip
    assert bookings.by_reference(ctx, "bk-xyz98765").id == "B-1"
    assert bookings.for_session(ctx, 1).id == "B-1"


def test_stats_keep_scorer_and_customer_apart(pipe):
    ctx = pipe.ctx
    now = T0 + timedelta(minutes=30)
    quotes.save(ctx, quote(1, status=QuoteStatus.ACCEPTED))
    quotes.save(ctx, quote(2, status=QuoteStatus.DECLINED))
    pipe.emit(ctx, "route", "router", session_id=1, payload={"kind": "customer_rfq"})
    pipe.emit(ctx, "route", "router", session_id=2, payload={"kind": "customer_rfq"})
    pipe.emit(ctx, "route", "router", session_id=3, payload={"kind": "customer_rfq"})
    pipe.emit(ctx, "quote.sent", "rfq", session_id=1)
    pipe.emit(ctx, "quote.fallback", "rfq", session_id=2)
    pipe.emit(ctx, "quote.missed", "rfq", session_id=3)
    pipe.emit(ctx, "llm.call", "llm", session_id=1, cost_usd=Decimal("0.002"), ts=now - timedelta(minutes=5))
    pipe.emit(ctx, "llm.call", "llm", session_id=9, cost_usd=Decimal("0.5"), ts=now - timedelta(hours=2))
    score = {"sent_at": T0.isoformat(), "points": {"accuracy": 45, "cost": 30, "time": 15}}
    pipe.emit(ctx, "score", "board", payload={**score, "round": 1, "verdict": "pending"})
    pipe.emit(ctx, "score", "board", payload={**score, "round": 1, "verdict": "accepted"})
    pipe.emit(ctx, "score", "board", payload={"round": 2, "verdict": "rejected", "points": {}})

    s = quotes.stats(ctx, now=now)
    assert (s["requests"], s["quoted"], s["fallback"], s["missed"]) == (3, 2, 1, 1)
    assert (s["accepted"], s["rejected"], s["won"], s["lost"]) == (1, 1, 1, 1)
    assert s["points_total"] == 90
    assert s["spend_per_accepted_usd"] == Decimal("0.0020")
    assert s["median_response_s"] == 90
