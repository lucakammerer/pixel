"""Tracking answers from the CRM alone (R25, R30): no gateway call, no status we do not hold."""

import re
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

from pixel.agents.rfq.compose import COMPANY, DEFAULT_SIGNER, ready_date
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
    "booking": "is with {carrier} for booking, and I am waiting for their reply",
    "booked": "is booked with {carrier}{under}; the airline's confirmation is still to come",
    "confirmed": "is booked with {carrier}, and the airline has confirmed it{under}",
    "failed": "could not be booked with {carrier} yet; a colleague is already on it and will be in touch",
}
# The record lines our answer carried before it was prose; kept verbatim until a variant is shown to score (R30).
RECORD = {
    "booking": "booking requested with {carrier}, waiting for the airline",
    "booked": "booked with {carrier}, waiting for the airline's confirmation",
    "confirmed": "confirmed by {carrier}, booked and ready to ship",
    "failed": "not booked: the booking with {carrier} failed and a colleague is on it",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
NAME = re.compile(r"^[A-ZÄÖÜ][a-zäöüß'’-]+(?: [A-ZÄÖÜ][a-zäöüß'’-]+){0,2}$")
NOT_A_NAME = re.compile(r"\b(?:Thanks|Thank|Regards|Cheers|Best|Kind|Many|Greetings|Team|Logistics|Shipping)\b")
FORMAL = re.compile(r"\b(?:dear|kind regards|yours sincerely|mit freundlichen)", re.IGNORECASE)
CASUAL = re.compile(r"^\s*(?:hi|hey|hallo)\b|\bcheers\b", re.IGNORECASE)
HELLO = {"formal": "Dear {full},", "neutral": "Hello {first},", "casual": "Hi {first},"}
BYE = {"formal": "Kind regards", "neutral": "Best regards", "casual": "Best wishes"}


class Detail(NamedTuple):
    ready: date | None = None
    postcode: str | None = None


def day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def greeting(text: str) -> tuple[str, str]:
    """Hello and goodbye in the asker's register, with the name they signed with."""
    register = "formal" if FORMAL.search(text) else "casual" if CASUAL.search(text) else "neutral"
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    for line in blocks[-1].splitlines() if blocks else []:
        line = line.strip().rstrip(",")
        if NAME.match(line) and not NOT_A_NAME.search(line):
            return HELLO[register].format(first=line.split()[0], full=line), BYE[register]
    return "Hello,", BYE[register]


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


def _paragraph(s: Shipment, detail: Detail, today: date) -> str:
    under = f" under booking reference {s.booking_reference}" if s.booking_reference else ""
    route = f"from {detail.postcode} via {s.origin}" if detail.postcode else f"from {s.origin}"
    status = STATUS.get(s.status, "is in status " + s.status).format(carrier=s.carrier, under=under)
    text = [f"Your shipment of {s.weight_kg} kg {route} to {s.destination} {status}."]
    if s.transit_days:
        transit = f"{s.transit_days} day{'' if s.transit_days == 1 else 's'} in transit"
        if detail.ready and s.status in ("booked", "confirmed"):
            arrive = day(detail.ready + timedelta(days=s.transit_days + (1 if detail.postcode else 0)))
            if detail.ready > today:
                text.append(f"It has not left yet: it will be ready on {day(detail.ready)}, and with {transit} it "
                            f"should arrive at {s.destination} on {arrive}.")  # fmt: skip
            else:
                text.append(f"It was ready on {day(detail.ready)}; with {transit} it is due at {s.destination} on "
                            f"{arrive}.")  # fmt: skip
        else:
            text.append(f"The routing takes {transit}.")
    text.append(f"Our own reference for it is {s.id}, in case you need it.")
    return " ".join(text)


def record(s: Shipment) -> str:
    lines = [
        f"Shipment {s.id}: {s.origin} → {s.destination}, {s.weight_kg} kg",
        f"Status: {RECORD.get(s.status, s.status).format(carrier=s.carrier)}",
    ]
    if s.booking_reference:
        lines.append(f"Airline booking reference: {s.booking_reference}")
    if s.transit_days:
        lines.append(f"Transit time: {s.transit_days} days")
    lines.append(f"Customer: {s.customer}")
    return "\n".join(lines)


def answer(
    found: list[Shipment],
    reference: str | None,
    question: str = "",
    details: dict[str, Detail] | None = None,
    today: date | None = None,
    signer: str = DEFAULT_SIGNER,
) -> str:
    """The status of each shipment in sentences, in the asker's register: status, airline, booking reference,
    transit and the arrival from the ready date, from the CRM alone; below it every record line the answer carried
    before, verbatim, until a variant is shown to score (R30)."""
    hello, bye = greeting(question)
    if not found:
        named = f" under {reference}" if reference else " matching your question"
        body = (f"I have checked, but we hold no shipment{named} in our records. Could you send me the airline's "
                "booking reference (it starts with BK-) or ours (PX-…)? I will answer right away.")  # fmt: skip
    else:
        today = today or datetime.now(UTC).date()
        details = details or {}
        body = "\n\n".join(_paragraph(s, details.get(s.id, Detail()), today) for s in found[:5])
        body += "\n\nHere is the status from our records:\n\n" + "\n\n".join(record(s) for s in found[:5])
    return f"{hello}\n\n{body}\n\nJust reply if you need anything else.\n\n{bye}\n{signer}\n{COMPANY}"


def details(ctx: Context, found: list[Shipment], question: str) -> dict[str, Detail]:
    """Ready date and pickup postcode of each shipment from the request it was quoted for; the question's own
    ready date when it names exactly one shipment."""
    out = {}
    for s in found[:5]:
        quote = quotes.for_session(ctx, s.session_id)
        row = quote and ctx.db.execute("SELECT body FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
        ready = ready_date(row["body"]) if row else None
        if ready is None and len(found) == 1:
            ready = ready_date(question)
        out[s.id] = Detail(ready, quote.rfq.pickup_postcode if quote and quote.rfq.door_pickup else None)
    return out


async def handle(ctx: Context, message: Message) -> None:
    """Finds the shipment the question means (a reference, or its description: origin or pickup postcode,
    destination, weight, company) in the CRM and
    answers in the thread from the CRM alone. Says so plainly when we hold no such shipment."""
    found, reference = match(ctx, f"{message.subject}\n{message.body}", message.sender)
    body = answer(found, reference, message.body, details(ctx, found, message.body), signer=ctx.settings.signer)
    events.emit(
        ctx, "tracking.answered", "tracking", session_id=message.session_id, message_id=message.id,
        payload={"reference": reference, "shipments": [s.id for s in found],
                 "shipment_sessions": [s.session_id for s in found], "statuses": [s.status for s in found]},
    )  # fmt: skip
    subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
    try:
        await outbox.send(
            ctx, to=message.sender, subject=subject, body=body, session_id=message.session_id,
            in_reply_to=message.id, purpose="tracking",
        )  # fmt: skip
    except outbox.NotSent:
        pass  # held by the outbox (dry run or a person took over); the draft waits in the sessions UI
