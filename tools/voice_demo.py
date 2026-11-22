"""Starts the UI on a local demo database with realistic booked orders so you can demonstrate the simulated
phone call and live laptop status updates without touching the 5U mailbox or gateway.

    uv run python -m tools.voice_demo

Then open:
- Laptop (audience view): http://localhost:8010/demo/new
- Phone screen (simulated phone): http://localhost:8010/phone"""

import argparse
import csv
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

KIT = Path("data/start-kit")
DB = "data/voice-demo.sqlite3"
US = "team@hackloop.mail"


def seed(ctx) -> int:
    """Seeds two realistic booked shipments/orders plus one open RFQ so both the Order Status Call demo
    (`/crm/shipments` and `/sessions/{id}`) and carrier/customer follow-up calls work immediately."""
    from pixel.contracts import Booking, BookingStatus, Preference, Quote, QuoteStatus, RateOption, Rfq, Shipment, Source
    from pixel.core import events
    from pixel.crm import bookings, quotes, shipments
    from pixel.pricing import store

    with (KIT / "carrier-list.csv").open(encoding="utf-8") as f:
        carriers = list(csv.DictReader(f))
    with (KIT / "pickup_zones.csv").open(encoding="utf-8") as f:
        store.save_zones(ctx, list(csv.DictReader(f)))
    store.save_carriers(ctx, carriers)

    now = datetime.now(UTC)
    first = (ctx.db.execute("SELECT MAX(id) FROM messages WHERE id < 1000000000").fetchone()[0] or 0) + 1

    # Seed two booked orders for the live pitch demo
    demo_orders = [
        {
            "sid": first,
            "ship_id": "PX-4F8A2C",
            "booking_id": "B-4F8A2C",
            "booking_ref": "BK-88412",
            "quote_id": "Q-4F8A2C",
            "customer": "nordlicht-audio@hackloop.mail",
            "subject": "Air freight FRA-JFK, 420 kg — Automotive sensors",
            "body": "Hi,\n\nPlease quote fastest air freight from FRA to JFK for 420 kg, 1.8 cbm.\n\nMira Petersen\nNordlicht Audio GmbH",
            "origin": "FRA",
            "destination": "JFK",
            "weight": Decimal("420"),
            "volume": Decimal("1.8"),
            "carrier": "Cobalt Skylift",
            "price": Decimal("1485.00"),
            "transit": 2,
            "status": "booked",
        },
        {
            "sid": first + 10,
            "ship_id": "PX-9C2E71",
            "booking_id": "B-9C2E71",
            "booking_ref": "BK-91044",
            "quote_id": "Q-9C2E71",
            "customer": "kessler-feinmechanik@hackloop.mail",
            "subject": "Urgent air export MUC-ORD, 265 kg — Optical lenses",
            "body": "Hello,\n\nWe need air freight from MUC to ORD, 265 kg, 1.1 cbm.\n\nJonas Kessler\nKessler Feinmechanik",
            "origin": "MUC",
            "destination": "ORD",
            "weight": Decimal("265"),
            "volume": Decimal("1.1"),
            "carrier": "Helix Air Freight",
            "price": Decimal("1120.50"),
            "transit": 3,
            "status": "in_transit",
        },
    ]

    for o in demo_orders:
        sid = o["sid"]
        ctx.db.execute(
            "INSERT OR REPLACE INTO sessions (id, customer, state, received_at, deadline_at) VALUES (?, ?, 'booked', ?, ?)",
            (sid, o["customer"], now.isoformat(), now.isoformat()),
        )
        ctx.db.execute(
            "INSERT OR REPLACE INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
            " in_reply_to, sent_at, data) VALUES (?, ?, 'in', 'customer_rfq', NULL, ?, ?, ?, ?, NULL, ?, '{}')",
            (sid, sid, o["customer"], US, o["subject"], o["body"], now.isoformat()),
        )
        rfq = Rfq(
            session_id=sid,
            message_id=sid,
            customer=o["customer"],
            origin=o["origin"],
            destination=o["destination"],
            weight_kg=o["weight"],
            volume_cbm=o["volume"],
            preference=Preference.FASTEST,
            received_at=now,
            deadline_at=now,
        )
        opt = RateOption(
            source=Source.PORTAL,
            carrier=o["carrier"],
            origin=o["origin"],
            destination=o["destination"],
            chargeable_kg=o["weight"],
            billed_kg=o["weight"],
            freight=o["price"],
            total=o["price"],
            currency="EUR",
            total_eur=o["price"],
            transit_days=o["transit"],
        )
        quote = Quote(
            id=o["quote_id"],
            session_id=sid,
            rfq=rfq,
            option=opt,
            total=o["price"],
            currency="EUR",
            subject=f"Re: {o['subject']}",
            body=f"All-in rate {o['price']} EUR via {o['carrier']}.",
            status=QuoteStatus.ACCEPTED,
            created_at=now,
            sent_at=now,
        )
        quotes.save(ctx, quote)
        booking = Booking(
            id=o["booking_id"],
            quote_id=o["quote_id"],
            session_id=sid,
            team_reference=o["ship_id"],
            via="portal",
            carrier=o["carrier"],
            booking_reference=o["booking_ref"],
            price=o["price"],
            currency="EUR",
            status=BookingStatus.CONFIRMED,
            created_at=now,
            confirmed_at=now,
        )
        bookings.save(ctx, booking)
        shipment = Shipment(
            id=o["ship_id"],
            session_id=sid,
            booking_id=o["booking_id"],
            customer=o["customer"],
            origin=o["origin"],
            destination=o["destination"],
            weight_kg=o["weight"],
            carrier=o["carrier"],
            booking_reference=o["booking_ref"],
            status=o["status"],
            transit_days=o["transit"],
            updated_at=now,
        )
        shipments.save(ctx, shipment)
        events.emit(ctx, "mail.in", "poller", session_id=sid, message_id=sid,
                    payload={"from": o["customer"], "subject": o["subject"], "sent_at": now.isoformat()})  # fmt: skip
        events.emit(ctx, "rfq.extracted", "rfq", session_id=sid, payload=rfq.model_dump(mode="json"))
        events.emit(ctx, "booking.confirmed", "rfq", session_id=sid,
                    payload={"booking_id": o["booking_id"], "booking_reference": o["booking_ref"]})  # fmt: skip

    return first


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    # Before pixel.config reads them: no mailbox, no gateway, nothing sent.
    os.environ.update({"PIXEL_DB": DB, "PIXEL_DRY_RUN": "1", "PIXEL_API_KEY": "", "OPENAI_API_KEY": "",
                       "PIXEL_QUOTE_TARGET_S": "600", "PIXEL_QUOTE_HARD_S": "900"})  # fmt: skip
    Path(DB).unlink(missing_ok=True)

    import uvicorn
    from fastapi import Request
    from fastapi.responses import RedirectResponse

    from pixel.agents.rfq import extract
    from pixel.app import app
    from pixel.config import settings

    extract.ASK_WAIT_S = 600
    if not (settings().elevenlabs_api_key and settings().elevenlabs_agent_id):
        raise SystemExit("Set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID in .env (uv run python -m tools.voice_agent).")

    @app.get("/demo/new", include_in_schema=False)
    async def new(request: Request):
        seed(request.app.state.ctx)
        return RedirectResponse("/crm/shipments", status_code=303)

    # Seed automatically on first startup so refreshing /crm/shipments directly never shows an empty CRM.
    orig_lifespan = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def demo_lifespan(a):
        async with orig_lifespan(a):
            seed(a.state.ctx)
            yield

    app.router.lifespan_context = demo_lifespan

    print(f"Laptop (audience view): http://localhost:{args.port}/crm/shipments")
    print(f"Fake phone screen:      http://localhost:{args.port}/phone")
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
