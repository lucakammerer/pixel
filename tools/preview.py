"""The UI on synthetic data, for looking at it locally. No poller, no gateway, no mail: dry run is forced.

uv run python -m tools.preview                    # seeds data/preview.sqlite3 once, serves http://localhost:8000
uv run python -m tools.preview --reseed --count 60
uv run python -m tools.preview --every 30         # a new request plays through every 30 s (0 turns it off)

Carriers, destinations, origins and truckers come from the start kit's carrier list and pickup zones; customers,
rates and outcomes are invented. Prices here are random, not computed by `pricing.calc`: nothing here is a quote.
"""

import argparse
import asyncio
import csv
import json
import random
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from pixel.config import settings
from pixel.contracts import (
    Booking,
    BookingStatus,
    MailKind,
    Message,
    Quote,
    QuoteStatus,
    RateOption,
    Rfq,
    Source,
)  # fmt: skip
from pixel.core import db, events
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.ui import crm, dashboard, routes

KIT = Path("data/start-kit")
US = "pixel@hackloop.mail"
COMPANIES = ("acme", "globex", "initech", "umbrella", "hooli", "stark", "wayne", "tyrell", "cyberdyne", "soylent",
             "wonka", "oscorp", "aperture", "vandelay")  # fmt: skip
ROLES = ("ops", "logistics", "export", "shipping", "freight", "buyer")
FATES = ("quoted",) * 5 + ("booked",) * 5 + ("confirmed",) * 4 + ("declined",) * 3 + ("fallback",) * 2 + (
    "failed", "missed")  # fmt: skip
CENT = Decimal("0.01")


class Kit:
    """What the synthetic data is drawn from: the start kit, so no lane or airline lives in code."""

    def __init__(self, folder: Path):
        with open(folder / "carrier-list.csv") as f:
            rows = list(csv.DictReader(f))
        with open(folder / "pickup_zones.csv") as f:
            zones = list(csv.DictReader(f))
        self.airlines = [r for r in rows if r["kind"] == "airline"]
        self.truckers = [r for r in rows if r["kind"] == "trucker"]
        self.origins = sorted({z["airport"] for z in zones})
        self.zones = zones


class Seeder:
    def __init__(self, ctx: Context, kit: Kit, rng: random.Random):
        self.ctx, self.kit, self.rng = ctx, kit, rng
        self.customers = [f"{rng.choice(ROLES)}@{c}.example" for c in COMPANIES]
        self.next_id = (ctx.db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 4000) + 1
        self.round = ctx.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'score'").fetchone()[0]

    # -- storage helpers -------------------------------------------------------------------------------------
    def emit(self, at: datetime | None, kind: str, actor: str, **kw):
        event = events.emit(self.ctx, kind, actor, **kw)
        if at:
            self.ctx.db.execute("UPDATE events SET ts = ? WHERE id = ?", (at.isoformat(), event.id))
        return event

    def mail(self, at, session_id, direction, sender, to, subject, body, kind=None, reply_to=None) -> Message:
        mid = self.next_id
        self.next_id += 1
        session_id = session_id or mid
        m = Message(id=mid, session_id=session_id, direction=direction, sender=sender, to=to, subject=subject,
                    body=body, in_reply_to=reply_to, sent_at=at, kind=kind)  # fmt: skip
        self.ctx.db.execute(
            "INSERT INTO messages (id, session_id, direction, kind, purpose, sender, recipient, subject, body,"
            " in_reply_to, sent_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, session_id, direction, kind, None, sender, to, subject, body, reply_to, at.isoformat(),
             m.model_dump_json()),
        )  # fmt: skip
        self.emit(at, f"mail.{direction}", "poller" if direction == "in" else "outbox", session_id=session_id,
                  message_id=mid)  # fmt: skip
        return m

    # -- one request -----------------------------------------------------------------------------------------
    def request(self, t0: datetime, fate: str, *, live_deadline: bool = False) -> int:
        r = self.rng
        customer = r.choice(self.customers)
        airline = r.choice(self.kit.airlines)
        destination = r.choice(airline["serves"].split())
        origin = r.choice(self.kit.origins)
        kg = Decimal(r.choice([45, 80, 120, 150, 230, 310, 450, 520, 760, 880, 1200]))
        cbm = Decimal(r.choice([0, 0, 0, "0.8", "1.6", "2.4"]))
        door = r.random() < 0.25
        zone = r.choice([z for z in self.kit.zones if z["airport"] == origin]) if door else None
        fastest = r.random() < 0.2
        subject = f"Air freight {origin}-{destination}, {kg} kg"
        body = (
            f"Hello,\n\nplease quote {kg} kg general cargo"
            + (f", {cbm} cbm" if cbm else "")
            + f" from {origin} to {destination}"
            + (f", door pickup in {zone['area']} ({zone['zone']}xxx)" if zone else ", airport to airport")
            + (". We need it there as fast as possible." if fastest else ".")
            + "\n\nKind regards"
        )
        m = self.mail(t0, None, "in", customer, US, subject, body, kind=MailKind.CUSTOMER_RFQ)
        sid = m.session_id
        deadline = (datetime.now(UTC) + timedelta(seconds=r.randint(150, 300))) if live_deadline else (
            t0 + timedelta(minutes=6))  # fmt: skip
        self.ctx.db.execute(
            "INSERT INTO sessions (id, customer, state, mode, received_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, customer, "missed" if fate == "missed" else "open", "human" if fate == "human" else "auto",
             t0.isoformat(), deadline.isoformat()),
        )  # fmt: skip
        at = t0 + timedelta(seconds=2)
        self.emit(at, "route", "router", session_id=sid, payload={"kind": "customer_rfq"})
        by_model = r.random() < 0.3
        if by_model:
            self.emit(at + timedelta(seconds=1), "llm.call", "llm", session_id=sid,
                      payload={"model": "gpt-5-mini", "purpose": "extract"}, tokens_in=r.randint(600, 1100),
                      tokens_out=r.randint(60, 160), cost_usd=str(Decimal(r.uniform(0.0002, 0.0007)).quantize(
                          Decimal("0.00001"))), duration_ms=r.randint(600, 1800))  # fmt: skip
        self.emit(at + timedelta(seconds=2), "rfq.extracted", "rfq", session_id=sid,
                  payload={"origin": origin, "destination": destination, "weight_kg": str(kg),
                           "door_pickup": door, "by": "model" if by_model else "rules"})  # fmt: skip
        rfq = Rfq(session_id=sid, message_id=sid, customer=customer, origin=origin, destination=destination,
                  weight_kg=kg, volume_cbm=cbm, preference="fastest" if fastest else "cheapest", door_pickup=door,
                  pickup_postcode=f"{zone['zone']}123" if zone else None, pickup_zone=zone and zone["zone"],
                  received_at=t0, deadline_at=t0 + timedelta(minutes=6), extracted_by="model" if by_model else "rules")  # fmt: skip

        options = self.options(sid, rfq, at + timedelta(seconds=4))
        if fate in ("open", "missed") or not options:
            if fate == "missed":
                self.emit(t0 + timedelta(minutes=6), "quote.missed", "orchestrator", session_id=sid)
            return sid
        best = min(options, key=lambda o: (o.transit_days, o.total_eur) if fastest else (o.total_eur,))
        self.emit(at + timedelta(seconds=9), "llm.call", "llm", session_id=sid,
                  payload={"model": "gpt-5-mini", "purpose": "compose"}, tokens_in=r.randint(400, 700),
                  tokens_out=r.randint(120, 260), cost_usd="0.00038", duration_ms=r.randint(700, 1600))  # fmt: skip
        sent_at = t0 + timedelta(seconds=r.randint(330, 345) if fate == "fallback" else r.randint(45, 190))
        text = (
            f"Hello,\n\nthank you for your request. For {kg} kg {origin} to {destination}"
            + (" including pickup" if door else "")
            + f" we offer:\n\nCarrier: {best.carrier}\nTransit: {best.transit_days} days\n"
            f"All-in total: {best.currency} {best.total}\n\nReply to this mail to book.\n\nBest regards,\nPIXEL Air Freight"
        )
        status = {"declined": QuoteStatus.DECLINED, "human": QuoteStatus.DRAFT}.get(fate, QuoteStatus.SENT)
        if fate in ("booked", "confirmed", "failed"):
            status = QuoteStatus.ACCEPTED
        q = Quote(id=f"Q-{r.getrandbits(32):08X}", session_id=sid, rfq=rfq, option=best, total=best.total,
                  currency=best.currency, subject=f"Re: {subject}", body=text, status=status,
                  fallback=fate == "fallback", llm_calls=1 + by_model, llm_cost_usd=Decimal("0.0008"),
                  created_at=at + timedelta(seconds=10))  # fmt: skip
        if fate == "human":
            quotes.save(self.ctx, q)
            self.emit(at + timedelta(seconds=10), "quote.drafted", "rfq", session_id=sid, payload={"quote": q.id})
            self.emit(at + timedelta(seconds=20), "human.takeover", "human", session_id=sid)
            self.emit(at + timedelta(seconds=21), "mail.held", "outbox", session_id=sid,
                      payload={"to": customer, "subject": q.subject, "body": text, "reason": "human", "quote": q.id})  # fmt: skip
            return sid
        out = self.mail(sent_at, sid, "out", US, customer, q.subject, text, reply_to=sid)
        q = q.model_copy(update={"sent_at": sent_at, "sent_message_id": out.id})
        quotes.save(self.ctx, q)
        self.ctx.db.execute("UPDATE sessions SET state = 'quoted', quote_message_id = ? WHERE id = ?", (out.id, sid))
        self.emit(sent_at, "quote.fallback" if fate == "fallback" else "quote.sent", "rfq", session_id=sid,
                  payload={"quote": q.id, "total": str(q.total), "currency": q.currency})  # fmt: skip
        self.score(q, sent_at)
        if fate == "declined":
            reply = self.mail(sent_at + timedelta(minutes=r.randint(3, 20)), sid, "in", customer, US,
                              f"Re: {q.subject}", "Thanks, but we found a cheaper option this time.",
                              kind=MailKind.CUSTOMER_REPLY, reply_to=out.id)  # fmt: skip
            self.emit(reply.sent_at, "customer.declined", "crm", session_id=sid, message_id=reply.id)
        elif fate in ("booked", "confirmed", "failed"):
            self.book(q, out, sent_at + timedelta(minutes=r.randint(2, 15)), fate)
        return sid

    def options(self, sid: int, rfq: Rfq, at: datetime) -> list[RateOption]:
        r = self.rng
        serving = [a for a in self.kit.airlines if rfq.destination in a["serves"].split()]
        chargeable = max(rfq.weight_kg, (rfq.volume_cbm * 167).quantize(CENT))
        found = []
        for i, airline in enumerate(r.sample(serving, min(len(serving), r.randint(2, 4)))):
            source = r.choice([Source.SHEET, Source.SHEET, Source.PORTAL, Source.BID, Source.RATEDESK])
            when = at + timedelta(seconds=3 * i)
            if source == Source.BID:
                self.emit(when, "bid.requested", "rfq", session_id=sid, payload={"to": airline["email"]})
                when += timedelta(seconds=r.randint(20, 70))
                self.mail(when, sid, "in", airline["email"], US, f"Re: Rate request {rfq.origin}-{rfq.destination}",
                          f"Our offer for {chargeable} kg: see rates below.", kind=MailKind.CARRIER_BID)  # fmt: skip
            if r.random() < 0.08:
                self.emit(when, "option.failed", "pricing", session_id=sid,
                          payload={"carrier": airline["name"], "why": "no rate for this weight"})  # fmt: skip
                continue
            freight = max(Decimal(r.randint(45, 120)), (chargeable * Decimal(r.uniform(1.7, 5.2))).quantize(CENT))
            fuel = (chargeable * Decimal(r.uniform(0.4, 1.1))).quantize(CENT)
            security = (chargeable * Decimal(r.uniform(0.1, 0.25))).quantize(CENT)
            pickup = Decimal(r.randint(60, 180)) if rfq.door_pickup else Decimal(0)
            total = freight + fuel + security + pickup
            option = RateOption(
                source=source, carrier=airline["name"], origin=rfq.origin, destination=rfq.destination,
                chargeable_kg=chargeable, billed_kg=chargeable, freight=freight, fuel=fuel, security=security,
                pickup=pickup, trucker=r.choice(self.kit.truckers)["name"] if rfq.door_pickup else None, total=total,
                currency="EUR", total_eur=total, transit_days=r.randint(1, 6),
                rate_id=f"R-{r.getrandbits(40):010x}" if source == Source.PORTAL else None,
            )  # fmt: skip
            found.append(option)
            self.emit(when + timedelta(seconds=1), "option.found", "pricing", session_id=sid,
                      payload={"option": option.model_dump(mode="json")})  # fmt: skip
        return found

    def book(self, q: Quote, out: Message, at: datetime, fate: str) -> None:
        r = self.rng
        reply = self.mail(at, q.session_id, "in", q.rfq.customer, US, f"Re: {q.subject}",
                          "Great, please book it.", kind=MailKind.CUSTOMER_REPLY, reply_to=out.id)  # fmt: skip
        self.emit(at, "customer.accepted", "crm", session_id=q.session_id, message_id=reply.id)
        shipment = shipments.create(self.ctx, q)
        ok = fate != "failed"
        status = {"booked": BookingStatus.BOOKED, "confirmed": BookingStatus.CONFIRMED}.get(fate, BookingStatus.FAILED)
        booked_at = at + timedelta(seconds=r.randint(5, 40))
        booking = Booking(
            id=f"B-{shipment.id}", quote_id=q.id, session_id=q.session_id, team_reference=shipment.id,
            via="portal" if q.option.rate_id else "email", carrier=q.option.carrier, rate_id=q.option.rate_id,
            booking_reference=f"BK-{r.getrandbits(32):08X}" if ok else None, price=q.total, currency=q.currency,
            status=status, created_at=booked_at,
            confirmed_at=booked_at + timedelta(minutes=r.randint(5, 30)) if fate == "confirmed" else None,
        )  # fmt: skip
        bookings.save(self.ctx, booking)
        shipments.save(self.ctx, shipment.model_copy(update={
            "booking_id": booking.id, "booking_reference": booking.booking_reference, "status": str(status),
            "updated_at": booking.confirmed_at or booked_at,
        }))  # fmt: skip
        self.emit(booked_at - timedelta(seconds=3), "booking.requested", "crm", session_id=q.session_id,
                  payload={"shipment": shipment.id, "rate_id": q.option.rate_id, "carrier": q.option.carrier})  # fmt: skip
        if not ok:
            self.emit(booked_at, "booking.failed", "crm", session_id=q.session_id,
                      payload={"why": "rate expired, no fresh rate", "shipment": shipment.id})  # fmt: skip
            return
        self.emit(booked_at, "booking.booked", "crm", session_id=q.session_id,
                  payload={"shipment": shipment.id, "booking_reference": booking.booking_reference})  # fmt: skip
        self.mail(booked_at + timedelta(seconds=2), q.session_id, "out", US, q.rfq.customer, f"Booked: {q.subject}",
                  f"Hello,\n\nyour shipment {shipment.id} is booked with {q.option.carrier}, airline reference "
                  f"{booking.booking_reference}.\n\nBest regards,\nPIXEL Air Freight", reply_to=reply.id)  # fmt: skip
        if fate == "confirmed":
            self.emit(booking.confirmed_at, "booking.confirmed", "crm", session_id=q.session_id,
                      payload={"booking_reference": booking.booking_reference})  # fmt: skip

    def score(self, q: Quote, sent_at: datetime) -> None:
        r = self.rng
        self.round += 1
        verdict = r.choices(["accepted", "rejected", "pending"], [0.78, 0.17, 0.05])[0]
        points = {"accuracy": round(r.uniform(3.8, 4.5), 1), "cost": round(r.uniform(2.6, 4.0), 1),
                  "time": round(max(0.2, 1.5 - (sent_at - q.rfq.received_at).total_seconds() / 300), 1)}  # fmt: skip
        if verdict != "accepted":
            points = {k: 0 for k in points}
        reason = f"{r.uniform(5.2, 11):.1f}% above the best price" if verdict == "rejected" else ""
        self.emit(sent_at + timedelta(seconds=30), "score", "board",
                  payload={"round": self.round, "sent_at": sent_at.isoformat(), "verdict": verdict, "reason": reason,
                           "points": points, "total": round(sum(points.values()), 1)})  # fmt: skip

    def tracking(self, at: datetime) -> None:
        found = shipments.recent(self.ctx, 100)
        booked = [s for s in found if s.booking_reference]
        if not booked:
            return
        s = self.rng.choice(booked)
        m = self.mail(at, None, "in", s.customer, US, f"Where is {s.id}?",
                      f"Hi, can you tell me the status of shipment {s.id}?", kind=MailKind.TRACKING_QUESTION)  # fmt: skip
        self.emit(at + timedelta(seconds=2), "route", "router", session_id=m.session_id,
                  payload={"kind": "tracking_question"})  # fmt: skip
        self.mail(at + timedelta(seconds=20), m.session_id, "out", US, s.customer, f"Re: Where is {s.id}?",
                  f"Hello,\n\n{s.id} is {s.status} with {s.carrier}, reference {s.booking_reference}.\n\nPIXEL",
                  reply_to=m.id)  # fmt: skip
        self.emit(at + timedelta(seconds=21), "tracking.answered", "tracking", session_id=m.session_id,
                  payload={"shipment": s.id, "status": s.status})  # fmt: skip

    def history(self, count: int) -> None:
        """`count` requests over the past hours, one every 6 minutes, ending with two still open now."""
        now = datetime.now(UTC)
        for i in range(count):
            t0 = now - timedelta(minutes=6 * (count - i) + 4)
            self.request(t0, self.rng.choice(FATES))
            if i % 7 == 6:
                self.tracking(t0 + timedelta(minutes=3))
        self.request(now - timedelta(seconds=150), "human", live_deadline=True)
        self.request(now - timedelta(seconds=40), "open", live_deadline=True)


async def play(seeder: Seeder, every: float) -> None:
    """A new request every `every` seconds, its events spaced out in real time so the live view has something."""
    while True:
        await asyncio.sleep(every)
        r = seeder.rng
        fate = r.choice(("quoted", "quoted", "booked", "declined", "fallback"))
        sid = seeder.request(datetime.now(UTC), "open", live_deadline=True)
        await asyncio.sleep(r.uniform(8, 20))
        await _finish(seeder, sid, fate)


async def _finish(seeder: Seeder, sid: int, fate: str) -> None:
    """Sends the quote for a session `play` opened, then maybe books it, all with the time of now."""
    ctx = seeder.ctx
    rows = ctx.db.execute("SELECT payload FROM events WHERE session_id = ? AND kind = 'option.found'", (sid,))
    options = [RateOption.model_validate(json.loads(r["payload"])["option"]) for r in rows]
    if not options:
        return
    session = ctx.db.execute("SELECT customer FROM sessions WHERE id = ?", (sid,)).fetchone()
    m = ctx.db.execute("SELECT data FROM messages WHERE id = ?", (sid,)).fetchone()
    first = Message.model_validate_json(m["data"])
    best = min(options, key=lambda o: o.total_eur)
    now = datetime.now(UTC)
    rfq = Rfq(session_id=sid, message_id=sid, customer=session["customer"], origin=best.origin,
              destination=best.destination, weight_kg=best.chargeable_kg, received_at=first.sent_at,
              deadline_at=first.sent_at + timedelta(minutes=6))  # fmt: skip
    text = f"Hello,\n\nall-in {best.currency} {best.total} with {best.carrier}, {best.transit_days} days.\n\nPIXEL"
    out = seeder.mail(now, sid, "out", US, rfq.customer, f"Re: {first.subject}", text, reply_to=sid)
    q = Quote(id=f"Q-{seeder.rng.getrandbits(32):08X}", session_id=sid, rfq=rfq, option=best, total=best.total,
              currency=best.currency, subject=f"Re: {first.subject}", body=text, fallback=fate == "fallback",
              status=QuoteStatus.SENT, created_at=now, sent_at=now, sent_message_id=out.id)  # fmt: skip
    quotes.save(ctx, q)
    ctx.db.execute("UPDATE sessions SET state = 'quoted' WHERE id = ?", (sid,))
    seeder.emit(None, "quote.fallback" if fate == "fallback" else "quote.sent", "rfq", session_id=sid,
                payload={"quote": q.id, "total": str(q.total), "currency": q.currency})  # fmt: skip
    seeder.score(q, now)
    if fate == "booked":
        await asyncio.sleep(6)
        quotes.save(ctx, q.model_copy(update={"status": QuoteStatus.ACCEPTED}))
        seeder.book(q.model_copy(update={"status": QuoteStatus.ACCEPTED}), out, datetime.now(UTC), "booked")
    elif fate == "declined":
        await asyncio.sleep(6)
        quotes.save(ctx, q.model_copy(update={"status": QuoteStatus.DECLINED}))
        seeder.emit(None, "customer.declined", "crm", session_id=sid)


def build(path: Path, *, reseed: bool, count: int, seed: int) -> tuple[Context, Seeder]:
    if path.resolve() == Path(settings().db_path).resolve():
        raise SystemExit(f"refusing to write synthetic data into the configured database {path}")
    if reseed:
        for p in path.parent.glob(path.name + "*"):
            p.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(path))
    db.init(conn)
    config = replace(settings(), db_path=str(path), api_key="", dry_run=True)
    ctx = Context(settings=config, db=conn, mailbox=None, portal=None, gateway=None, board=None)
    seeder = Seeder(ctx, Kit(KIT), random.Random(seed))
    if not conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
        seeder.history(count)
        print(f"seeded {count} requests into {path}")
    return ctx, seeder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/preview.sqlite3", type=Path)
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--count", default=40, type=int, help="requests to seed")
    parser.add_argument("--seed", default=7, type=int, help="random seed")
    parser.add_argument("--every", default=45.0, type=float, help="seconds between live requests; 0 for none")
    parser.add_argument("--reseed", action="store_true", help="delete the database and seed it again")
    args = parser.parse_args()
    ctx, seeder = build(args.db, reseed=args.reseed, count=args.count, seed=args.seed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(play(seeder, args.every)) if args.every > 0 else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="PIXEL preview", lifespan=lifespan)
    for module in (routes, dashboard, crm):
        app.include_router(module.router)
    app.state.ctx = ctx
    print(f"preview on http://localhost:{args.port} (synthetic data, nothing is sent)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
