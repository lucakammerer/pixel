"""A customer's later mail in their thread: what it changes and what we do with it, whatever its place."""

from decimal import Decimal

import pytest
from test_crm import mail, quote

from pixel.agents.rfq import agent, booking, followup
from pixel.contracts import Booking, BookingStatus, QuoteStatus
from pixel.core import db, events
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import engine

pytest_plugins = ["test_crm"]  # the `pipe` fixture

ROUND_86 = (
    "Sorry for the change, the final packing list came in, and the shipment details have changed to:\n\n"
    "5 pieces, 1317 kg gross weight, 8.18 cbm\n\nEverything else stays the same. Could you please update your offer?"
)


def notes(ctx):
    return [r[0] for r in ctx.db.execute("SELECT payload FROM events WHERE kind = 'human.note'")]


@pytest.fixture
def requoted(pipe, monkeypatch):
    """`agent.requote` replaced by a recorder: which mail it answers and with which request."""
    calls = []

    async def requote(ctx, message, rfq):
        calls.append((message.id, rfq))

    monkeypatch.setattr(agent, "requote", requote)
    return calls


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (ROUND_86, {"weight_kg": "1317", "volume_cbm": "8.18", "pieces": "5"}),
        (
            "Kleine Korrektur: es sind jetzt 4 Paletten mit 980 kg und 5,2 cbm.",
            {"weight_kg": "980", "volume_cbm": "5.2", "pieces": "4"},
        ),
        (
            "Correction : le poids brut est maintenant de 1 120 kg, volume 6,1 m3.",
            {"weight_kg": "1120", "volume_cbm": "6.1"},
        ),
        ("Perdón, el peso correcto es 900 kg y 5 cbm.", {"weight_kg": "900", "volume_cbm": "5"}),
        ("Please change the destination to BKK instead of JFK.", {"destination": "BKK"}),
        ("Please quote transport to BKK airport instead of JFK.", {"destination": "BKK"}),  # no truck leg after a quote
        ("Could you deliver to BKK airport instead of New York?", {"destination": "BKK"}),
        ("Actually please deliver them to BKK airport.", {"destination": "BKK"}),
        (
            "Please collect from our warehouse in 80331 Munich instead.",
            {"door_pickup": "True", "pickup_postcode": "80331", "pickup_zone": "80"},
        ),
        ("Actually we need the fastest option instead.", {"preference": "fastest"}),
        ("Could you quote in USD instead?", {"currency": "USD"}),
        # Nothing changes: a hypothetical, our own figures quoted back, a signature's postcode, the same lane.
        ("What would it cost if it were 1000 kg?", {}),
        ("150 kg chargeable is fine, go ahead.", {}),
        ("Thanks again!\n\nKlaus Weidner\nHauptstr. 1, 90402 Nürnberg", {}),
        ("Thanks for confirming the booking from FRA to JFK.", {}),
        ("Please book the fastest option.", {}),
        (
            "Wir liefern die Ware selbst zum Flughafen FRA.",
            {},
        ),  # the truck to the departure airport, never the destination
        ("Wir fahren die Kisten morgen zum Flughafen FRA.", {}),
        ("Für den Flug liefern wir die Ware selbst zum Flughafen FRA.", {}),  # the air word anywhere before
        ("Wir bringen die Luftfracht selbst zum Flughafen FRA.", {}),
        ("Die Luftfrachtsendung liefern wir selbst zum Flughafen FRA.", {}),
        ("We deliver ourselves to FRA airport.", {}),
        ("Our forwarder will truck it to the airport FRA, destination stays JFK.", {}),
    ],
)
def test_changes_are_the_fields_a_mail_states_differently(text, want):
    rfq = quote(1).rfq
    assert {k: str(v) for k, v in followup.changes(rfq, text, {Decimal(150)}).items()} == want


async def test_a_change_before_our_quote_is_quoted_in_reply_to_it(pipe, requoted):
    # The request is read but not yet quoted: the correction must price the quote, not wait for a person.
    pipe.emit(
        pipe.ctx, "rfq.extracted", "rfq", session_id=1, message_id=1, payload=quote(1).rfq.model_dump(mode="json")
    )
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: RFQ", body="Sorry, it is 320 kg, not 150."))
    ((answered, rfq),) = requoted
    assert answered == 20 and rfq.weight_kg == Decimal(320) and notes(pipe.ctx) == []


async def test_an_acceptance_with_a_change_is_quoted_again_never_booked_at_the_old_weight(pipe, requoted):
    quotes.save(pipe.ctx, quote(1))
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Your quote", body="Book it, but it is 320 kg now."))
    assert [(m, r.weight_kg) for m, r in requoted] == [(20, Decimal(320))]
    assert quotes.get(pipe.ctx, "Q-1").status == QuoteStatus.SENT


async def test_a_request_for_an_updated_offer_is_answered_also_when_no_price_field_changed(pipe, requoted):
    quotes.save(pipe.ctx, quote(1))
    body = "The cargo is now ready on 2026-11-30. Could you send me an updated offer?"
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Your quote", body=body))
    assert [m for m, _ in requoted] == [20]


async def test_accepting_the_updated_offer_books_it(pipe, requoted, monkeypatch):
    quotes.save(pipe.ctx, quote(1))
    seen = []

    async def book(ctx, quote_id, reply_to=None, *, resume=False):
        seen.append(quote_id)

    monkeypatch.setattr(booking, "book", book)
    body = "Thanks, the updated offer works for us, please go ahead and book it."
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Your quote", body=body))
    assert requoted == [] and seen == ["Q-1"]


async def test_a_change_after_the_booking_goes_to_a_person_with_what_changed(pipe, requoted):
    quotes.save(pipe.ctx, quote(1, status=QuoteStatus.ACCEPTED))
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Booked", body="Sorry, it is 320 kg now."))
    (note,) = notes(pipe.ctx)
    assert requoted == [] and '"weight_kg": "320"' in note and '"phase": "accepted"' in note


async def test_a_thank_you_after_the_booking_needs_nobody(pipe):
    quotes.save(pipe.ctx, quote(1, status=QuoteStatus.ACCEPTED))
    body = "Thank you for arranging the booking. Please let me know once the airline confirms."
    await booking.handle_reply(pipe.ctx, mail(20, 1, subject="Re: Booked", body=body))
    assert notes(pipe.ctx) == [] and pipe.sent == []


async def test_a_tracking_question_in_the_customers_thread_is_answered_from_its_shipment(pipe):
    ctx = pipe.ctx
    ctx.db.execute(
        "INSERT INTO sessions (id, customer, state, received_at) VALUES (1, 'buyer@acme.example', 'quoted', '')"
    )
    quotes.save(ctx, quote(1, status=QuoteStatus.ACCEPTED))
    shipment = shipments.create(ctx, quote(1))
    shipments.save(ctx, shipment.model_copy(update={"booking_reference": "BK-AB12CD34", "status": "confirmed"}))
    bookings.save(ctx, Booking(id="B-1", quote_id="Q-1", session_id=1, team_reference=shipment.id, via="portal",
                               carrier="Kestrel Airways", booking_reference="BK-AB12CD34", status=BookingStatus.CONFIRMED,
                               created_at=quote(1).created_at))  # fmt: skip
    await booking.handle_reply(ctx, mail(20, 1, subject="Re: Booked", body="Where is our shipment now?"))
    (answer,) = pipe.sent
    assert answer["purpose"] == "tracking" and answer["in_reply_to"] == 20 and shipment.id in answer["body"]
    assert notes(ctx) == []


async def test_a_requote_holds_the_quote_of_the_older_facts_and_answers_with_a_price_it_holds(pipe, monkeypatch):
    # No fresh option for the changed request: the best price the session holds still answers (R1).
    first = quote(1)
    pipe.emit(
        pipe.ctx, "option.found", "pricing", session_id=1, payload={"option": first.option.model_dump(mode="json")}
    )
    seen = {}

    async def options(ctx, rfq, deadline):
        from pixel.core import outbox

        seen["quoted"] = outbox.quoted(ctx, 1)
        return []

    monkeypatch.setattr(engine, "options", options)
    monkeypatch.setattr(agent, "_check", lambda *a: None)
    sent = await agent.requote(pipe.ctx, mail(20, 1), first.rfq.model_copy(update={"weight_kg": Decimal(320)}))
    assert seen["quoted"] and pipe.quoted == [(20, first.option.total)] and sent is not None
    assert db.get(pipe.ctx.db, "requote:1") is None
    assert "followup.read" in events.KINDS
