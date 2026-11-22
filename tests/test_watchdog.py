from datetime import timedelta

from pixel.contracts import MailKind
from pixel.core import events, feedback, poller, timing, watchdog
from tests.test_extract import RECEIVED, mail
from tests.test_outbox import make_ctx


async def test_a_request_open_past_45_seconds_is_alerted_once_with_its_step(conn, monkeypatch):
    posted = []

    async def post(ctx, issues):
        posted.append(issues)

    monkeypatch.setattr(feedback, "post", post)
    ctx = make_ctx(conn, alert_url="https://hook.test/x")
    poller.store(
        ctx, mail("Please quote AAA-BBB, 100 kg, door pickup.").model_copy(update={"kind": MailKind.CUSTOMER_RFQ})
    )
    at = RECEIVED
    monkeypatch.setattr(timing, "now", lambda: at)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=7)
    events.emit(ctx, "bid.requested", "pricing", session_id=7, message_id=70, payload={"pickup": True})
    at = RECEIVED + timedelta(seconds=44)
    assert await watchdog.once(ctx) == 0
    at = RECEIVED + timedelta(seconds=46)  # from when the mail was stored, however late we polled it
    assert await watchdog.once(ctx) == 1
    assert {k: posted[0][0][k] for k in ("issue", "session", "age_s", "step")} == {
        "issue": "stuck", "session": 7, "age_s": 46, "step": "pickup"}  # fmt: skip
    at = RECEIVED + timedelta(seconds=150)
    assert await watchdog.once(ctx) == 0 and len(posted) == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'quote.stuck'").fetchone()[0] == 1


async def test_a_quote_the_last_reader_holds_is_not_stuck_until_its_hold_ends(conn, monkeypatch):
    ctx = make_ctx(conn)
    poller.store(ctx, mail("Please quote AAA-BBB, 100 kg.").model_copy(update={"kind": MailKind.CUSTOMER_RFQ}))
    until = RECEIVED + timedelta(seconds=100)
    events.emit(ctx, "quote.unmet", "rfq", session_id=7, payload={"hold_until": until.isoformat()})
    monkeypatch.setattr(timing, "now", lambda: RECEIVED + timedelta(seconds=60))
    assert await watchdog.once(ctx) == 0  # `unmet.settle` alerted it itself; it leaves at the target
    monkeypatch.setattr(timing, "now", lambda: until + timedelta(seconds=10))
    assert await watchdog.once(ctx) == 1
