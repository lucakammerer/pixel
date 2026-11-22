"""The voice calls of `pixel/voice/` on a demo request: the real app on its own database, no poller, no gateway, no
mail. Each request has a late airline, a late trucker and an unanswered question to its customer, so the request's page
shows the Call panel. Needs ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID in .env (`tools.voice_agent`).

uv run python -m tools.voice_demo                 # http://localhost:8010, a fresh request at /demo/new

The deadlines are stretched to 15 minutes and the question's wait to 10, so a call can be tried at leisure.
"""

import argparse
import csv
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

DB = "data/voice-demo.sqlite3"
KIT = Path("data/start-kit")
US = "pixel@hackloop.mail"


def seed(ctx) -> int:
    """One new request with its bid requests and its question, all sent just now. Returns its session id."""
    from pixel.contracts import Rfq
    from pixel.core import events
    from pixel.pricing import store

    with (KIT / "carrier-list.csv").open(encoding="utf-8") as f:
        carriers = list(csv.DictReader(f))
    with (KIT / "pickup_zones.csv").open(encoding="utf-8") as f:
        store.save_zones(ctx, list(csv.DictReader(f)))
    store.save_carriers(ctx, carriers)
    airline = next(c for c in carriers if c["kind"] == "airline" and "ORD" in c["serves"].split())
    trucker = next(c for c in carriers if c["kind"] == "trucker" and "60" in c["serves"].split())
    now = datetime.now(UTC)
    first = (ctx.db.execute("SELECT MAX(id) FROM messages WHERE id < 1000000000").fetchone()[0] or 0) + 1
    customer = "demo-buyer@hackloop.mail"
    mails = [
        (
            first,
            "in",
            None,
            customer,
            US,
            "Air freight FRA-ORD, 420 kg",
            "Hello, please quote door pickup in 60311 Frankfurt to ORD, 420 kg, 6 pallets. Thank you.",
        ),
        (
            first + 1,
            "out",
            "bid_request",
            US,
            airline["email"],
            "Rate request FRA-ORD, 420 kg",
            "Hello,\n\nplease quote air freight for this shipment:\nFrom: FRA\nTo: ORD\n...",
        ),
        (
            first + 2,
            "out",
            "bid_pickup",
            US,
            trucker["email"],
            "Pickup request 60311 to FRA, 420 kg",
            "Hello,\n\nplease quote a pickup ...",
        ),
        (
            first + 3,
            "out",
            "question",
            US,
            customer,
            "Re: Air freight FRA-ORD, 420 kg",
            "Hello,\n\nthank you for your request. Could you tell us the volume or the dimensions of the pieces? ...",
        ),
    ]
    for id_, direction, purpose, sender, to, subject, body in mails:
        ctx.db.execute(
            "INSERT INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
            " in_reply_to, sent_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
            (id_, first, direction, "customer_rfq" if id_ == first else None, purpose, sender, to, subject, body,
             None if id_ == first else first, now.isoformat()),
        )  # fmt: skip
    events.emit(ctx, "mail.in", "poller", session_id=first, message_id=first,
                payload={"from": customer, "subject": mails[0][5], "sent_at": now.isoformat()})  # fmt: skip
    request = Rfq(session_id=first, message_id=first, customer=customer, origin="FRA", destination="ORD",
                  weight_kg=Decimal(420), pieces=6, door_pickup=True, pickup_postcode="60311", pickup_zone="60",
                  received_at=now, deadline_at=now)  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=first, payload=request.model_dump(mode="json"))
    for mail, carrier, pickup in [(mails[1], airline, False), (mails[2], trucker, True)]:
        events.emit(ctx, "bid.requested", "pricing", session_id=first, message_id=mail[0],
                    payload={"carrier": carrier["name"], "email": carrier["email"], "pickup": pickup,
                             "origin": "FRA", "destination": "ORD"})  # fmt: skip
    events.emit(ctx, "mail.out", "outbox", session_id=first, message_id=first + 3,
                payload={"to": customer, "subject": mails[3][5], "purpose": "question"})  # fmt: skip
    return first


def main() -> None:
    parser = argparse.ArgumentParser()
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
        return RedirectResponse(f"/sessions/{seed(request.app.state.ctx)}#calls", status_code=303)

    print(f"Voice demo: http://localhost:{args.port}/demo/new")
    uvicorn.run(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
