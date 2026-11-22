"""Voice calls (`pixel/voice/calls.py`): what a carrier or customer says on the phone reaches the thread as a mailed
answer would, our rules read it, and the waits for it stay open while the call goes on."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from pixel.config import settings
from pixel.contracts import Message, Rfq, Source
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, store
from pixel.voice import calls

CUSTOMER, AIRLINE, TRUCKER = "buyer@example.com", "sales@air.example", "desk@truck.example"


@pytest.fixture
def ctx(conn, monkeypatch):
    config = replace(settings(), elevenlabs_api_key="el-key", elevenlabs_agent_id="agent", dry_run=True, bid_wait_s=0)
    ctx = Context(config, conn, None, None, None, None)
    now = datetime.now(UTC)
    store.save_carriers(ctx, [
        {"name": "Example Air", "kind": "airline", "email": AIRLINE, "serves": "BBB"},
        {"name": "Example Truck", "kind": "trucker", "email": TRUCKER, "serves": "12"},
    ])  # fmt: skip
    for row in [
        (1, "in", None, CUSTOMER, "us@example.com", "Air freight AAA-BBB"),
        (2, "out", "bid_request", "us@example.com", AIRLINE, "Rate request AAA-BBB, 100 kg"),
        (3, "out", "bid_pickup", "us@example.com", TRUCKER, "Pickup request 12345"),
    ]:
        conn.execute(
            "INSERT INTO messages (id, session_id, direction, purpose, sender, recipient, subject, body, sent_at, data)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, '', ?, '{}')",
            (row[0], row[1], row[2], row[3], row[4], row[5], now.isoformat()),
        )
    request = Rfq(session_id=1, message_id=1, customer=CUSTOMER, origin="AAA", destination="BBB", weight_kg=D(100),
                  door_pickup=True, pickup_postcode="12345", pickup_zone="12", received_at=now, deadline_at=now)  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    for message_id, email, name, pickup in [(2, AIRLINE, "Example Air", False), (3, TRUCKER, "Example Truck", True)]:
        events.emit(ctx, "bid.requested", "pricing", session_id=1, message_id=message_id,
                    payload={"carrier": name, "email": email, "pickup": pickup, "origin": "AAA", "destination": "BBB"})  # fmt: skip

    async def signed(ctx, path):
        return {"signed_url": "wss://example.invalid/call"}

    monkeypatch.setattr(calls, "_el", signed)
    return ctx


def _rfq() -> Rfq:
    now = datetime.now(UTC)
    return Rfq(session_id=1, message_id=1, customer=CUSTOMER, origin="AAA", destination="BBB", weight_kg=D(100),
               received_at=now, deadline_at=now)  # fmt: skip


@pytest.mark.parametrize(
    "pickup, fields, total, currency, days",
    [
        (False, {"total": "1195.90", "currency": "eur", "transit_days": "3"}, D("1195.90"), "EUR", 3),
        (False, {"total": "980", "currency": "EUR", "transit_days": 2}, D(980), "EUR", 2),
        (True, {"total": "85.00", "currency": "EUR", "pickup_hours": "12"}, D("85.00"), "EUR", 0),
    ],
)
def test_a_phoned_offer_is_written_as_our_rules_read_a_mailed_one(pickup, fields, total, currency, days):
    target = calls.Target("carrier", "Example", "x@example.com", 2, pickup)
    text = calls.body(target, fields)
    mail = Message(id=1, session_id=1, direction="in", sender="x@example.com", to="us", subject="Re", body=text,
                   sent_at=datetime.now(UTC))  # fmt: skip
    option = bids.parse(mail, _rfq(), needs_transit=not pickup)
    assert (option.total, option.currency, option.transit_days) == (total, currency, days)
    if pickup:
        assert bids._pickup_hours(text) == 12


@pytest.mark.parametrize(
    "fields",
    [
        {"total": "", "currency": "EUR", "transit_days": "3"},
        {"total": "-5", "currency": "EUR", "transit_days": "3"},
        {"total": "900", "currency": "euro", "transit_days": "3"},
        {"total": "900", "currency": "EUR"},
    ],
)
def test_an_unclear_offer_is_refused_and_not_guessed(fields):
    with pytest.raises(calls.CallError):
        calls.body(calls.Target("carrier", "Example", "x@example.com", 2), fields)


def test_a_customer_answer_holds_only_the_facts_we_asked_for():
    target = calls.Target("customer", CUSTOMER, CUSTOMER, 5, missing=("weight", "pickup"))
    text = calls.body(target, {"weight_kg": "450", "pickup_postcode": "80331", "destination": "JFK"})
    assert "The gross weight is 450 kg." in text and "Pickup postcode: 80331." in text and "JFK" not in text
    with pytest.raises(calls.CallError):
        calls.body(target, {"destination": "JFK"})


async def test_a_late_airline_is_called_and_its_phoned_offer_becomes_a_bid(ctx):
    assert {t.email for t in calls.targets(ctx, 1)} == {AIRLINE, TRUCKER}
    started = await calls.start(ctx, 1, AIRLINE)
    assert started["dynamic_variables"]["lane"] == "AAA to BBB"
    assert calls.holding(ctx, 1, "carrier") and not calls.holding(ctx, 1, "customer")
    with pytest.raises(calls.CallError):
        await calls.start(ctx, 1, TRUCKER)  # one carrier call at a time

    message_id = calls.answer(ctx, started["call_id"], {"total": "1195.90", "currency": "EUR", "transit_days": "3"})
    await asyncio.gather(*ctx.tasks)
    received = [e.payload for e in events.for_session(ctx, 1) if e.kind == "bid.received"]
    assert received[0]["carrier"] == "Example Air" and received[0]["request_id"] == 2
    found = [e.payload["option"] for e in events.for_session(ctx, 1) if e.kind == "option.found"]
    assert found[0]["source"] == Source.BID.value and D(found[0]["total"]) == D("1195.90")
    assert message_id > calls.VOICE_IDS
    with pytest.raises(calls.CallError):
        calls.answer(ctx, started["call_id"], {"total": "1000", "currency": "EUR", "transit_days": "3"})
    assert {t.email for t in calls.targets(ctx, 1)} == {TRUCKER}


async def test_collect_waits_for_a_bid_while_the_carrier_is_on_the_phone(ctx):
    now = datetime.now(UTC)
    deadline = Deadline(received_at=now - timedelta(seconds=60), target_at=now, hard_at=now + timedelta(seconds=5),
                        expires_at=now + timedelta(seconds=300))  # fmt: skip
    assert await bids.collect(ctx, 1, deadline, [2]) == []  # nobody on the phone: the target has passed
    started = await calls.start(ctx, 1, AIRLINE)

    async def phoned():
        await asyncio.sleep(0.5)
        calls.answer(ctx, started["call_id"], {"total": "800", "currency": "EUR", "transit_days": "4"})

    found, _ = await asyncio.gather(bids.collect(ctx, 1, deadline, [2]), phoned())
    assert [(o.carrier, o.total) for o in found] == [("Example Air", D("800.00"))]


async def test_an_ended_call_releases_the_wait(ctx):
    started = await calls.start(ctx, 1, AIRLINE)
    calls.ended(ctx, started["call_id"])
    events.emit(ctx, "error", "test", session_id=1)  # later events do not revive it
    later = datetime.now(UTC) + timedelta(seconds=calls.END_GRACE_S + 1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calls.timing, "now", lambda: later)
        assert not calls.holding(ctx, 1, "carrier")


def test_nobody_is_called_once_the_quote_is_out(ctx, monkeypatch):
    monkeypatch.setattr("pixel.core.outbox.quoted", lambda ctx, session_id: True)
    assert calls.targets(ctx, 1) == []


def test_the_elevenlabs_key_never_lands_in_an_event(ctx):
    event = events.emit(ctx, "error", "test", payload={"error": "ElevenLabs rejected el-key"})
    assert "el-key" not in str(event.payload)


async def test_the_question_waits_for_the_customer_on_the_phone_and_reads_their_answer(ctx, monkeypatch):
    from pixel.agents.rfq import extract
    from pixel.core import outbox

    async def send(ctx, *, to, subject, body, session_id, in_reply_to=None, purpose):
        ctx.db.execute(
            "INSERT INTO messages (id, session_id, direction, purpose, sender, recipient, subject, body, sent_at, data)"
            " VALUES (4, ?, 'out', ?, 'us@example.com', ?, ?, ?, ?, '{}')",
            (session_id, purpose, to, subject, body, datetime.now(UTC).isoformat()),
        )
        return Message(id=4, session_id=session_id, direction="out", sender="us@example.com", to=to, subject=subject,
                       body=body, sent_at=datetime.now(UTC))  # fmt: skip

    monkeypatch.setattr(outbox, "send", send)
    monkeypatch.setattr(extract, "ASK_WAIT_S", 0.3)
    now = datetime.now(UTC)
    request = Message(id=1, session_id=1, direction="in", sender=CUSTOMER, to="us@example.com",
                      subject="Air freight AAA-BBB", body="", sent_at=now)  # fmt: skip
    deadline = Deadline(received_at=now, target_at=now + timedelta(seconds=5), hard_at=now + timedelta(seconds=10),
                        expires_at=now + timedelta(seconds=300))  # fmt: skip

    async def phone():
        while not (target := next((t for t in calls.targets(ctx, 1) if t.party == "customer"), None)):
            await asyncio.sleep(0.02)
        assert target.missing == ("weight",)
        started = await calls.start(ctx, 1, CUSTOMER)
        await asyncio.sleep(0.6)  # past the question's own wait
        calls.answer(ctx, started["call_id"], {"weight_kg": "450"})

    said, _ = await asyncio.gather(extract._ask(ctx, request, _rfq(), ["weight"], deadline), phone())
    assert "The gross weight is 450 kg." in said


def test_the_session_page_offers_a_call_only_when_voice_is_set_up(ctx):
    from pixel.ui import routes

    page = routes._session_page(ctx, 1, "")
    assert "data-call-email='sales@air.example'" in page and "/voice/call.js" in page
    ctx.settings = replace(ctx.settings, elevenlabs_agent_id="")
    assert "voice-calls" not in routes._session_page(ctx, 1, "")
