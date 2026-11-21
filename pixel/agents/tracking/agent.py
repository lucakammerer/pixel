"""Tracking answers from the CRM alone (R25, R30): no gateway call, no status we do not hold."""

import re

from pixel.agents.rfq.extract import number
from pixel.contracts import Message, Shipment
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import quotes, shipments

REFERENCE = re.compile(r"\b(?:BK-[A-Z0-9]{4,}|PX-[A-F0-9]{6})\b", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LANE = re.compile(r"\b([A-Z]{3})\s*(?:-|–|→|->|to|nach)\s*([A-Z]{3})\b")
DESCRIBED = re.compile(r"\b(?:([A-Z]{3})|(\d{5}))\s+(?:to|nach)\s+([A-Z]{3})\b")
WEIGHT = re.compile(r"(\d[\d.,']*)\s*kg\b", re.IGNORECASE)

STATUS = {
    "booking": "booking requested with {carrier}, waiting for the airline",
    "booked": "booked with {carrier}, waiting for the airline's confirmation",
    "confirmed": "confirmed by {carrier}, booked and ready to ship",
    "failed": "not booked: the booking with {carrier} failed and a colleague is on it",
}


def described(ctx: Context, text: str) -> list[Shipment]:
    """The tracking desk names no reference. It describes the shipment the way the customer asked for it: the origin
    airport, or the pickup postcode of a door-to-port request, "to" the destination, the gross weight and the
    customer's company name. Each of these narrows the shipments to that destination; one of them must fit."""
    lane = DESCRIBED.search(text)
    if not lane:
        return []
    origin, postcode, destination = lane.groups()
    weight = WEIGHT.search(text)
    kg = number(weight.group(1)) if weight else None
    words = text.lower()

    def from_there(s: Shipment) -> bool:
        if origin:
            return s.origin == origin
        quote = quotes.for_session(ctx, s.session_id)
        return bool(quote and quote.rfq.pickup_postcode == postcode)

    def company(s: Shipment) -> bool:
        tokens = [t for t in re.split(r"[^a-z0-9]+", s.customer.split("@")[0].lower()) if len(t) > 2]
        return bool(tokens) and all(t in words for t in tokens)

    found, fits = shipments.find(ctx, destination=destination), False
    for check in (from_there, lambda s: kg is not None and s.weight_kg == kg, company):
        narrowed = [s for s in found if check(s)]
        if narrowed:
            found, fits = narrowed, True
    return found if fits else []


def match(ctx: Context, text: str, sender: str) -> tuple[list[Shipment], str | None]:
    """Shipments the question means, and the reference it named (if any)."""
    references = [m.group(0).upper() for m in REFERENCE.finditer(text)]
    for reference in references:
        found = shipments.find(ctx, reference=reference)
        if found:
            return found, reference
    found = described(ctx, text)
    if found:
        return found, None
    customers = [e for e in EMAIL.findall(text) if e.lower() != sender.lower()]
    lane = LANE.search(text)
    origin, destination = lane.groups() if lane else (None, None)
    for customer in customers or [None]:
        if customer or lane:
            found = shipments.find(ctx, customer=customer, origin=origin, destination=destination)
            if found:
                return found, references[0] if references else None
    return [], references[0] if references else None


def answer(found: list[Shipment], reference: str | None) -> str:
    if not found:
        named = f" under {reference}" if reference else " matching your question"
        return (
            f"Hello,\n\nwe hold no shipment{named} in our records. Could you send us the booking reference "
            "(BK-…) or our reference (PX-…)? We will answer right away.\n\nBest regards,\nPIXEL Air Freight"
        )
    parts = []
    for s in found[:5]:
        lines = [
            f"Shipment {s.id}: {s.origin} → {s.destination}, {s.weight_kg} kg",
            f"Status: {STATUS.get(s.status, s.status).format(carrier=s.carrier)}",
        ]
        if s.booking_reference:
            lines.append(f"Airline booking reference: {s.booking_reference}")
        if s.transit_days:
            lines.append(f"Transit time: {s.transit_days} days")
        lines.append(f"Customer: {s.customer}")
        parts.append("\n".join(lines))
    return (
        "Hello,\n\nhere is the status from our records:\n\n"
        + "\n\n".join(parts)
        + "\n\nBest regards,\nPIXEL Air Freight"
    )


async def handle(ctx: Context, message: Message) -> None:
    """Finds the shipment the question means (a reference, or its description: origin or pickup postcode,
    destination, weight, company) in the CRM and
    answers in the thread from the CRM alone. Says so plainly when we hold no such shipment."""
    found, reference = match(ctx, f"{message.subject}\n{message.body}", message.sender)
    events.emit(
        ctx, "tracking.answered", "tracking", session_id=message.session_id, message_id=message.id,
        payload={"reference": reference, "shipments": [s.id for s in found],
                 "shipment_sessions": [s.session_id for s in found], "statuses": [s.status for s in found]},
    )  # fmt: skip
    subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
    try:
        await outbox.send(
            ctx, to=message.sender, subject=subject, body=answer(found, reference), session_id=message.session_id,
            in_reply_to=message.id, purpose="tracking",
        )  # fmt: skip
    except outbox.NotSent:
        pass  # held by the outbox (dry run or a person took over); the draft waits in the sessions UI
