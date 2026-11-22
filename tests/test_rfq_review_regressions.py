import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from pixel.agents.rfq import agent, extract
from pixel.contracts import Attachment, LaneTariff
from pixel.core import events, orchestrator, poller
from pixel.core.orchestrator import deadline_for
from pixel.crm import bookings, quotes
from pixel.pricing import bids, calc, engine, portal_rates, session, store
from tests.test_extract import DEADLINE, mail
from tests.test_outbox import make_ctx, make_quote
from tests.test_rfq_airports import answers, request, served


class PackingList:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    async def attachment(self, message_id, attachment_id):
        self.reads += 1
        return self.text.encode()


def with_attachment(message):
    doc = Attachment(id=1, filename="packing.txt", content_type="text/plain", size=100)
    return message.model_copy(update={"attachments": (doc,)})


async def test_mail_weight_and_attached_volume_both_reach_pricing(conn):
    ctx = make_ctx(conn, PackingList("Total volume: 10 cbm"))
    msg = with_attachment(mail("Please quote AAA-BBB, total gross weight 100 kg. See the attached packing list."))
    rfq = await extract.extract(ctx, msg, DEADLINE)
    assert (rfq.weight_kg, rfq.volume_cbm, calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)) == (100, 10, 1670)
    [sources] = [e.payload["sources"] for e in events.for_session(ctx, msg.id) if e.kind == "rfq.evidence"]
    assert sources["weight_kg"] == ["mail"] and sources["volume_cbm"] == ["packing.txt"]
    assert ctx.mailbox.reads == 1 and rfq.extracted_by == "rules"


def test_recovered_weight_is_ready_for_pricing():
    msg = mail("Please quote air freight from the factory to BBB, 100 kg gross.")
    parsed = extract._from_model({"origin": "AAA", "destination": "BBB", "weight_kg": None}, msg, DEADLINE)
    assert parsed.weight_kg == 100 and "weight_kg" not in parsed.missing


async def test_pickup_postcode_in_the_instructions_is_used_for_sourcing(conn):
    ctx = make_ctx(conn, PackingList("Pickup from 12345 Example City"))
    msg = with_attachment(mail("Please quote AAA-BBB, 100 kg. Pickup required."))
    rfq = await extract.extract(ctx, msg, DEADLINE)
    assert (rfq.door_pickup, rfq.pickup_zone, rfq.pickup_postcode) == (True, "12", "12345")
    assert "pickup_postcode" not in rfq.missing


async def test_only_current_mail_and_attachment_support_guessed_airports(conn, monkeypatch):
    ctx = served(conn)
    ctx.mailbox = PackingList("Destination: ddd")
    msg = with_attachment(
        mail("Air freight from our usual airport, 100 kg.\n\n-----Original Message-----\nFrom XXX to CCC.")
    )
    msg = msg.model_copy(update={"subject": "RE: From XXX to CCC"})
    answers(monkeypatch, {"origin": "XXX", "destination": "DDD", "weight_kg": "100"})
    rfq, text = await extract.read_request(ctx, msg, DEADLINE)
    assert rfq.origin == "XXX" and "XXX" not in text
    answers(monkeypatch, {"origin": "AAA", "destination": None})
    corrected = await agent.served_airports(ctx, rfq, msg, text=text)
    assert (corrected.origin, corrected.destination) == ("AAA", "DDD")
    assert ctx.mailbox.reads == 1
    ctx.mailbox = PackingList("Origin: xxx\nDestination: ddd")
    _, text = await extract.read_request(ctx, msg, DEADLINE)
    called = answers(monkeypatch, {})
    explicit = await agent.served_airports(ctx, request("XXX", "DDD"), msg, text=text)
    assert explicit.origin == "XXX" and not called


async def test_sheet_is_available_while_explicit_lane_waits_on_portal(conn, monkeypatch):
    ctx = make_ctx(conn)
    msg = mail("Please quote AAA-BBB, 50 kg.")
    store.upsert(ctx, [LaneTariff(
        carrier="Test carrier", origin="AAA", destination="BBB", currency="EUR",
        valid_from=date(2000, 1, 1), valid_to=date(2100, 1, 1), version="example", transit_days=2,
        minimum=Decimal(5), breaks=((Decimal(0), Decimal(2)),), fuel_per_kg=Decimal(0),
        security_per_kg=Decimal(0), source="fixture",
    )])  # fmt: skip
    reading, resume = asyncio.Event(), asyncio.Event()

    async def lanes(ctx, **kwargs):
        reading.set()
        await resume.wait()
        return []

    monkeypatch.setattr(portal_rates, "lanes", lanes)
    work = asyncio.create_task(agent.handle(ctx, msg, deadline_for(datetime.now(UTC), ctx.settings)))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        quote = await agent.fallback(ctx, msg.session_id)
        assert quote and quote.total == 100
    finally:
        resume.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_optional_validation_returns_to_sourcing_with_slow_portal_and_model(conn, monkeypatch):
    ctx = served(conn)
    called = []

    async def slow_portal(*args):
        called.append("portal")
        await asyncio.Event().wait()

    async def slow_model(*args, **kwargs):
        called.append("model")
        await asyncio.Event().wait()

    monkeypatch.setattr(portal_rates, "lanes", slow_portal)
    monkeypatch.setattr(agent.llm, "ask_json", slow_model)
    monkeypatch.setattr(agent, "AIRPORTS_WAIT_S", 0.04)
    original = request("XXX", "DDD")
    result = await asyncio.wait_for(agent.served_airports(ctx, original, mail("Freight to DDD.")), timeout=0.5)
    assert result == original and called == ["portal", "model"]


async def test_fallback_never_prices_the_previous_lane_after_an_airport_correction(conn):
    ctx = make_ctx(conn)
    msg = mail("Freight to BBB.")
    old = make_quote(msg)
    corrected = old.rfq.model_copy(update={"origin": "CCC"})
    events.emit(ctx, "rfq.extracted", "rfq", session_id=msg.session_id, payload=corrected.model_dump(mode="json"))
    events.emit(
        ctx,
        "option.found",
        "pricing",
        session_id=msg.session_id,
        payload={"option": old.option.model_dump(mode="json")},
    )
    assert await agent.fallback(ctx, msg.session_id, allow_partial=True) is None


async def test_a_corrected_lane_requests_a_fresh_bid_and_ignores_the_old_carrier_reply(conn, monkeypatch):
    ctx = make_ctx(conn, quote_target_s=0, bid_wait_s=0)
    msg = mail("Revised origin for this shipment.")
    old = make_quote(msg)
    corrected = old.rfq.model_copy(update={"origin": "CCC"})
    store.save_carriers(
        ctx, [{"name": old.option.carrier, "email": "one@example.com", "kind": "airline", "serves": "BBB"}]
    )
    events.emit(ctx, "rfq.extracted", "rfq", session_id=msg.id, payload=old.rfq.model_dump(mode="json"))
    # A pre-upgrade enquiry has no saved airport fields. Its preceding RFQ supplies them.
    events.emit(
        ctx,
        "bid.requested",
        "pricing",
        session_id=msg.id,
        message_id=50,
        payload={"email": "one@example.com", "carrier": old.option.carrier, "pickup": False},
    )
    events.emit(
        ctx, "option.found", "pricing", session_id=msg.id, payload={"option": old.option.model_dump(mode="json")}
    )
    events.emit(ctx, "rfq.extracted", "rfq", session_id=msg.id, payload=corrected.model_dump(mode="json"))

    async def offline(*args, **kwargs):
        return []

    monkeypatch.setattr(portal_rates, "lanes", offline)
    await bids.record(
        ctx,
        msg.model_copy(
            update={
                "id": 80,
                "sender": "one@example.com",
                "in_reply_to": 50,
                "body": "All-in EUR 12.00. Transit 3 days.",
            }
        ),
    )
    assert not await engine.options(ctx, corrected, deadline_for(datetime.now(UTC), ctx.settings))
    assert ctx.mailbox.sent == ["one@example.com"]
    assert not session.for_request(ctx, corrected)
    [old_answer] = [e.payload for e in events.for_session(ctx, msg.id) if e.kind == "bid.received"]
    assert old_answer["request_id"] == 50 and old_answer["ignored"]
    [current] = [e for e in events.for_session(ctx, msg.id) if e.kind == "bid.requested" and e.message_id != 50]
    await bids.record(
        ctx,
        msg.model_copy(
            update={
                "id": 81,
                "sender": "one@example.com",
                "in_reply_to": current.message_id,
                "body": "All-in EUR 280.00. Transit 3 days.",
            }
        ),
    )
    [offer] = session.for_request(ctx, corrected)
    assert (offer.origin, offer.destination, offer.total) == ("CCC", "BBB", 280)


async def test_bid_wait_counts_only_the_current_enquiries(conn, monkeypatch):
    ctx = make_ctx(conn)
    rfq = make_quote(mail("Freight")).rfq
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))

    def answer(request_id, origin):
        events.emit(
            ctx,
            "bid.received",
            "pricing",
            session_id=rfq.session_id,
            payload={
                "request_id": request_id,
                "origin": origin,
                "destination": rfq.destination,
                "pickup": False,
                "carrier": "Test Air",
            },
        )

    answer(10, rfq.origin)
    answer(10, rfq.origin)
    answer(11, "CCC")
    answer(9, rfq.origin)
    monkeypatch.setattr(bids, "POLL_S", 0.001)
    task = asyncio.create_task(
        bids.collect(ctx, rfq.session_id, deadline_for(datetime.now(UTC), ctx.settings), [10, 11])
    )
    try:
        await asyncio.sleep(0)
        assert not task.done()
        answer(11, rfq.origin)
        assert await asyncio.wait_for(task, timeout=1) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_further_shipments_bids_end_its_wait_on_its_own_lane(conn, monkeypatch):
    # Rounds 113, 127, 150 and 151: the bids were matched on the first shipment's lane and the quote waited until 100 s.
    ctx = make_ctx(conn)
    rfq = make_quote(mail("Freight")).rfq
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
    other = rfq.model_copy(update={"destination": "CCC"})
    events.emit(ctx, "bid.received", "pricing", session_id=rfq.session_id,
                payload={"request_id": 12, "origin": other.origin, "destination": "CCC", "pickup": False,
                         "carrier": "Test Air"})  # fmt: skip
    monkeypatch.setattr(bids, "POLL_S", 0.001)
    deadline = deadline_for(datetime.now(UTC), ctx.settings)
    assert await asyncio.wait_for(bids.collect(ctx, rfq.session_id, deadline, [12], rfq=other), timeout=1) == []


async def test_late_carrier_offer_is_quoted_once_and_the_acceptance_books_once(conn, monkeypatch):
    ctx = make_ctx(conn, quote_target_s=0, quote_hard_s=5, bid_wait_s=0)
    msg = mail("Please quote AAA to BBB, 100 kg, 0.3 cbm.").model_copy(update={"sent_at": datetime.now(UTC)})
    store.save_carriers(
        ctx, [{"name": "Test Air", "kind": "airline", "email": "test-air@example.com", "serves": "BBB"}]
    )

    async def no_portal_rates(*args, **kwargs):
        return []

    monkeypatch.setattr(portal_rates, "lanes", no_portal_rates)
    monkeypatch.setattr(orchestrator, "FALLBACK_POLL_S", 0.01)
    first = poller.ingest(ctx, msg)
    task = asyncio.create_task(orchestrator.dispatch(ctx, first))
    try:
        async with asyncio.timeout(2):
            while not any(e.kind == "quote.waiting" for e in events.for_session(ctx, first.id)):
                await asyncio.sleep(0.005)
        enquiry = conn.execute("SELECT id FROM messages WHERE purpose = 'bid_request'").fetchone()[0]
        response = msg.model_copy(
            update={
                "id": 100,
                "sender": "test-air@example.com",
                "subject": "Re: Rate request",
                "body": "Our all-in freight rate is EUR 2.80/kg. Transit 3 days.",
                "in_reply_to": enquiry,
            }
        )
        await orchestrator.dispatch(ctx, poller.ingest(ctx, response))
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    sent = quotes.for_session(ctx, msg.id)
    assert sent.fallback and sent.sent_at and sent.total == 280
    assert sent.body.count("280.00 EUR") == 1
    acceptance = msg.model_copy(
        update={"id": 101, "subject": "Re: quote", "body": "Please book it.", "in_reply_to": sent.sent_message_id}
    )
    for message_id in (101, 102):
        await orchestrator.dispatch(ctx, poller.ingest(ctx, acceptance.model_copy(update={"id": message_id})))
    assert bookings.for_session(ctx, msg.id).quote_id == sent.id
    assert [r[0] for r in conn.execute("SELECT purpose FROM messages WHERE direction='out' ORDER BY id")] == [
        "bid_request",
        "fallback_quote",
        "booking",
    ]


@pytest.mark.parametrize("language,scope", [("en", "pickup"), ("de", "abholung")])
async def test_early_fallback_waits_for_pickup_and_deadline_quote_labels_air_only(conn, language, scope):
    ctx = make_ctx(conn)
    msg = mail("Please quote my pickup")
    old = make_quote(msg)
    door = old.rfq.model_copy(update={"door_pickup": True, "language": language})
    events.emit(ctx, "rfq.extracted", "rfq", session_id=msg.session_id, payload=door.model_dump(mode="json"))
    events.emit(
        ctx,
        "option.found",
        "pricing",
        session_id=msg.session_id,
        payload={"option": old.option.model_dump(mode="json")},
    )
    assert await agent.fallback(ctx, msg.session_id) is None
    partial = await agent.fallback(ctx, msg.session_id, allow_partial=True)
    assert partial and scope in partial.body.lower() and "all-in" not in partial.body.lower()
    draft = next(e for e in events.for_session(ctx, msg.session_id) if e.kind == "quote.drafted")
    assert draft.payload["complete_service"] is False


@pytest.mark.parametrize(
    "doc",
    [
        "Packing list\nNet weight: 450 kg\n",
        "Packing list\nLength (cm) Width (cm) Height (cm) Pieces Net weight (kg)\n120 80 41 5 450\n",
        "Packliste\nLänge (cm) Breite (cm) Höhe (cm) Stück Nettogewicht (kg)\n120 80 41 5 450\n",
        "Packing list\nTare weight: 25 kg\n",
    ],
)
async def test_a_document_net_or_tare_weight_never_replaces_the_mail_gross(conn, doc):
    body = "Please quote FRA-ORD, 5 pallets, gross weight 500 kg, 2 cbm."
    rfq = await extract.extract(make_ctx(conn, PackingList(doc)), with_attachment(mail(body)), DEADLINE)
    assert rfq.weight_kg == Decimal(500)


async def test_every_shipment_of_one_mail_is_priced_in_the_one_quote(conn, monkeypatch):
    ctx = make_ctx(conn)
    store.upsert(ctx, [LaneTariff(
        carrier=f"Carrier {code}", origin="AAA", destination=code, currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="example", transit_days=2, minimum=Decimal(5),
        breaks=((Decimal(0), rate),), fuel_per_kg=Decimal(0), security_per_kg=Decimal(0), source="fixture",
    ) for code, rate in (("BBB", Decimal(2)), ("CCC", Decimal(3)))])  # fmt: skip

    async def no_lanes(ctx, **kwargs):
        return []

    monkeypatch.setattr(portal_rates, "lanes", no_lanes)
    answers(monkeypatch, {"origin": "AAA", "destination": "BBB", "weight_kg": "300", "preference": "fastest"})
    sent = []

    async def send_quote(ctx, quote, reply_to=None):
        sent.append(quote)
        return quote

    monkeypatch.setattr(agent.outbox, "send_quote", send_quote)
    body = "Please quote the fastest option from AAA for each of these shipments:\n1. BBB, 100 kg\n2. CCC, 200 kg"
    msg = poller.ingest(ctx, mail(body))
    received = datetime.now(UTC) - timedelta(seconds=ctx.settings.quote_target_s - 1)
    await asyncio.wait_for(agent.handle(ctx, msg, deadline_for(received, ctx.settings)), 10)
    [quote] = sent
    assert (quote.option.destination, quote.total) == ("BBB", Decimal(200))
    assert "600.00 EUR" in quote.body and "800.00 EUR" in quote.body


async def test_a_further_shipment_still_pricing_at_the_target_is_quoted_from_its_stored_prices(conn, monkeypatch):
    """Round 150: every TLV price was stored 30 s after the mail, but its pricing task still ran at the target (a
    rate read for the next break), so TLV was left out and the quote rejected as one_of_two."""
    ctx = make_ctx(conn)
    store.upsert(ctx, [LaneTariff(
        carrier=f"Carrier {code}", origin="AAA", destination=code, currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="example", transit_days=2, minimum=Decimal(5),
        breaks=((Decimal(0), rate),), fuel_per_kg=Decimal(0), security_per_kg=Decimal(0), source="fixture",
    ) for code, rate in (("BBB", Decimal(2)), ("CCC", Decimal(3)))])  # fmt: skip

    async def no_lanes(ctx, **kwargs):
        return []

    options = engine.options

    async def slow_for_ccc(ctx, rfq, deadline):
        found = await options(ctx, rfq, deadline)  # the sheet's price is stored as option.found
        if rfq.destination == "CCC":
            await asyncio.sleep(60)
        return found

    monkeypatch.setattr(portal_rates, "lanes", no_lanes)
    monkeypatch.setattr(engine, "options", slow_for_ccc)
    answers(monkeypatch, {"origin": "AAA", "destination": "BBB", "weight_kg": "300", "preference": "fastest"})
    sent = []

    async def send_quote(ctx, quote, reply_to=None):
        sent.append(quote)
        return quote

    monkeypatch.setattr(agent.outbox, "send_quote", send_quote)
    body = "Please quote the fastest option from AAA for each of these shipments:\n1. BBB, 100 kg\n2. CCC, 200 kg"
    msg = poller.ingest(ctx, mail(body))
    received = datetime.now(UTC) - timedelta(seconds=ctx.settings.quote_target_s - 1)
    await asyncio.wait_for(agent.handle(ctx, msg, deadline_for(received, ctx.settings)), 10)
    [quote] = sent
    assert "600.00 EUR" in quote.body and "800.00 EUR" in quote.body
