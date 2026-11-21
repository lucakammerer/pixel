"""Tracking answers from the CRM alone (R25, R30): no gateway call, no status we do not hold."""

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from pixel.agents.rfq import compose, extract
from pixel.agents.rfq.compose import COMPANY, DEFAULT_SIGNER, TEXT, in_kind, ready_date
from pixel.agents.rfq.extract import number
from pixel.contracts import Message, Shipment
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import quotes, shipments
from pixel.pricing.pickup import zone_of

REFERENCE = re.compile(r"\b(?:BK-[A-Z0-9]{4,}|PX-[A-F0-9]{6})\b", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LANE = re.compile(r"\b([A-Z]{3})\s*(?:-|–|→|->|to|nach)\s*([A-Z]{3})\b")
DESCRIBED = re.compile(r"\b(?:([A-Z]{3})|(\d{5}))(?:\s+(?:to|nach)\s+|\s*(?:-|–|→|->|/)\s*)([A-Z]{3})\b")
# "1795 kg", "1 795 kg", "1,795 kilos"
WEIGHT = re.compile(r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d[\d.,']*)\s*(?:kgs?|kilos?|kilograms?)\b", re.IGNORECASE)

# Which tracking answer goes out. Going back is this one line (bench/RUNS.md, "Tracking variants"):
# "scored": prose, then the record block with the customer's address; scored +10 on every right first answer.
# "a": the same without the address line, the pickup named by its town ("from your site in Bremen, flying HAM to EZE").
# "b": as "a", the record block's facts in the prose and no block.
# "c": "b" without the filler ("It has not left yet:", "in case you need it"), every fact kept.
VARIANT = "c"
TRANSLATE_WAIT_S = 15

STATUS = {
    "booking": "is with {carrier} for booking, and I am waiting for their reply",
    "booked": "is booked with {carrier}{under}; the airline's confirmation is still to come",
    "confirmed": "is booked with {carrier}, and the airline has confirmed it{under}",
    "failed": "could not be booked with {carrier} yet; a colleague is already on it and will be in touch",
}
# Variant "b" states the status in the record's words, the block's other facts are in the prose already.
CONFIRMED = "is confirmed by {carrier}{under}, booked and ready to ship"
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
    town: str | None = None


def day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def greeting(text: str) -> tuple[str, str]:
    """Hello and goodbye in the asker's register, with the name they signed with; the goodbye goes to the town they
    greet from, or is their own "Viele Grüße" or "Cheers", as in the quote's thread."""
    register = "formal" if FORMAL.search(text) else "casual" if CASUAL.search(text) else "neutral"
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    first = next((line.strip().rstrip(",") for line in (blocks[-1].splitlines() if blocks else [])
                  if NAME.match(line.strip().rstrip(",")) and not NOT_A_NAME.search(line)), None)  # fmt: skip
    own = in_kind(text, "en", register, first.split()[0] if first else None)[1]
    bye = own.rstrip(",") if own != TEXT["en"]["bye"][register] else BYE[register]
    return (HELLO[register].format(first=first.split()[0], full=first) if first else "Hello,"), bye


def _lane(ctx: Context, text: str) -> re.Match | None:
    """The first lane the desk names whose destination one of our shipments flies to: "ETD/ETA" is no lane."""
    return next((m for m in DESCRIBED.finditer(text) if shipments.find(ctx, destination=m.group(3))), None)


def described(ctx: Context, text: str) -> list[Shipment]:
    """The tracking desk names no reference. It describes the shipment the way the customer asked for it: the origin
    airport, or the pickup postcode of a door-to-port request, "to" the destination, the gross weight and the
    customer's company name. Origin (or the pickup zone) and weight must agree; when our origin differs from the
    desk's, the company and the weight alone may still name the one shipment."""
    named = _lane(ctx, text)
    if not named:
        return []
    origin, postcode, destination = named.groups()
    weight = WEIGHT.search(text)
    kg = number(weight.group(1)) if weight else None
    words = set(re.findall(r"[a-z0-9]+", text.lower()))

    def company(address: str) -> bool:
        """Every part of the mail's name in the desk's words, also inside one: "Kaffeeroesterei" for roesterei."""
        tokens = [t for t in re.split(r"[^a-z0-9]+", address.split("@")[0].lower()) if len(t) > 2]
        return bool(tokens) and all(any(t in w for w in words) for t in tokens)

    def weighs(candidates: list[Shipment]) -> list[Shipment]:
        if kg is None:
            return candidates
        exact_weight = [s for s in candidates if s.weight_kg == kg]
        tolerance = max(Decimal(1), kg * Decimal("0.01"))
        return exact_weight or [s for s in candidates if abs(s.weight_kg - kg) <= tolerance]

    addresses = {e.lower() for e in EMAIL.findall(text)}
    found = shipments.find(ctx, destination=destination)
    if addresses:
        found = [s for s in found if s.customer.lower() in addresses]
    names = [s for s in found if company(s.customer)]
    # A company the desk names that we know is never answered with another customer's shipment.
    known = any(company(r["customer"]) for r in ctx.db.execute("SELECT DISTINCT customer FROM sessions"))
    found = names if names or known else found
    if origin:
        strict = [s for s in found if s.origin == origin]
    else:
        exact, zone = [], []
        for shipment in found:
            quote = quotes.for_session(ctx, shipment.session_id)
            if quote is None or not quote.rfq.door_pickup:
                continue
            if quote.rfq.pickup_postcode == postcode:
                exact.append(shipment)
            elif quote.rfq.pickup_zone == zone_of(postcode):  # a remembered site of the customer, same zone
                zone.append(shipment)
        strict = exact or zone
    strict = weighs(strict)
    if len(strict) == 1:
        return strict
    # Our origin can differ from the desk's (the zone's airport replaced a guessed one): the company and the
    # weight alone still name one shipment.
    relaxed = weighs(names) if origin and names and kg is not None else []
    return relaxed if not strict and len(relaxed) == 1 else []


def match(ctx: Context, text: str, sender: str) -> tuple[list[Shipment], str | None]:
    """Identify one shipment before returning any customer or booking details."""
    references = list(dict.fromkeys(m.group(0).upper() for m in REFERENCE.finditer(text)))
    if references:
        sets = [shipments.find(ctx, reference=r) for r in references]
        if all(len(matches) == 1 for matches in sets) and len({matches[0].id for matches in sets}) == 1:
            return sets[0], references[0]
        return [], references[0]
    if _lane(ctx, text):
        return described(ctx, text.replace(sender, "")), None
    customers = {e.lower() for e in EMAIL.findall(text) if e.lower() != sender.lower()}
    lane = LANE.search(text)
    origin, destination = lane.groups() if lane else (None, None)
    if len(customers) > 1 or not (customers or lane):
        return [], None
    found = shipments.find(ctx, customer=next(iter(customers), None), origin=origin, destination=destination)
    return (found if len(found) == 1 else []), None


def _paragraph(s: Shipment, detail: Detail, today: date, variant: str) -> str:
    under = f" under booking reference {s.booking_reference}" if s.booking_reference else ""
    if variant == "scored":
        route = f"from {detail.postcode} via {s.origin} to {s.destination}" if detail.postcode else None
    else:
        site = f"your site in {detail.town}" if detail.town else "your site"
        route = f"from {site}, flying {s.origin} to {s.destination}," if detail.postcode else None
    short = variant == "c"
    statuses = {**STATUS, "confirmed": CONFIRMED} if variant in ("b", "c") else STATUS
    status = statuses.get(s.status, "is in status " + s.status).format(carrier=s.carrier, under=under)
    text = [f"Your shipment of {s.weight_kg} kg {route or f'from {s.origin} to {s.destination}'} {status}."]
    if s.transit_days:
        transit = f"{s.transit_days} day{'' if s.transit_days == 1 else 's'} in transit"
        if detail.ready and s.status in ("booked", "confirmed"):
            arrive = day(detail.ready + timedelta(days=s.transit_days + (1 if detail.postcode else 0)))
            if detail.ready > today and short:
                text.append(f"It will be ready on {day(detail.ready)} and, with {transit}, should arrive at "
                            f"{s.destination} on {arrive}.")  # fmt: skip
            elif detail.ready > today:
                text.append(f"It has not left yet: it will be ready on {day(detail.ready)}, and with {transit} it "
                            f"should arrive at {s.destination} on {arrive}.")  # fmt: skip
            else:
                text.append(f"It was ready on {day(detail.ready)}; with {transit} it is due at {s.destination} on "
                            f"{arrive}.")  # fmt: skip
        else:
            text.append(f"The routing takes {transit}.")
    text.append(f"Our reference is {s.id}." if short else f"Our own reference for it is {s.id}, in case you need it.")
    return " ".join(text)


def record(s: Shipment, variant: str) -> str:
    lines = [
        f"Shipment {s.id}: {s.origin} → {s.destination}, {s.weight_kg} kg",
        f"Status: {RECORD.get(s.status, s.status).format(carrier=s.carrier)}",
    ]
    if s.booking_reference:
        lines.append(f"Airline booking reference: {s.booking_reference}")
    if s.transit_days:
        lines.append(f"Transit time: {s.transit_days} days")
    if variant == "scored":
        lines.append(f"Customer: {s.customer}")
    return "\n".join(lines)


def answer(
    found: list[Shipment],
    reference: str | None,
    question: str = "",
    details: dict[str, Detail] | None = None,
    today: date | None = None,
    signer: str = DEFAULT_SIGNER,
    variant: str = VARIANT,
) -> str:
    """The status of each shipment in sentences, in the asker's register: status, airline, booking reference,
    transit and the arrival from the ready date, from the CRM alone; below it the record block, as `VARIANT` says
    (R30)."""
    hello, bye = greeting(question)
    if not found:
        named = f" under {reference}" if reference else " I can identify uniquely from those details"
        body = (f"I have checked, but we hold no shipment{named} in our records. Could you send me the airline's "
                "booking reference (it starts with BK-) or ours (PX-…)? I will answer right away.")  # fmt: skip
    else:
        today = today or datetime.now(UTC).date()
        details = details or {}
        body = "\n\n".join(_paragraph(s, details.get(s.id, Detail()), today, variant) for s in found[:5])
        if variant not in ("b", "c"):
            body += "\n\nHere is the status from our records:\n\n" + "\n\n".join(record(s, variant) for s in found[:5])
    return f"{hello}\n\n{body}\n\nJust reply if you need anything else.\n\n{bye}\n{signer}\n{COMPANY}"


def details(ctx: Context, found: list[Shipment], question: str) -> dict[str, Detail]:
    """Ready date, pickup postcode and its town (the pickup zone's area) of each shipment from the request it was
    quoted for; the question's own ready date and postcode when it names exactly one shipment."""
    out = {}
    for s in found[:5]:
        quote = quotes.for_session(ctx, s.session_id)
        row = quote and ctx.db.execute("SELECT body FROM messages WHERE id = ?", (quote.rfq.message_id,)).fetchone()
        ready = ready_date(row["body"]) if row else None
        if ready is None and len(found) == 1:
            ready = ready_date(question)
        postcode = quote.rfq.pickup_postcode if quote and quote.rfq.door_pickup else None
        named = _lane(ctx, question)
        if postcode and len(found) == 1 and named and named.group(2):
            postcode = named.group(2)  # the asker's own postcode, where ours was filled in from an earlier site
        area = (
            postcode and ctx.db.execute("SELECT area FROM pickup_zones WHERE zone = ?", (zone_of(postcode),)).fetchone()
        )
        out[s.id] = Detail(ready, postcode, area["area"] if area else None)
    return out


def _own_shipment(ctx: Context, message: Message) -> Shipment | None:
    """A customer asking in their own thread ("Where is our shipment?") means that thread's shipment."""
    row = ctx.db.execute("SELECT customer FROM sessions WHERE id = ?", (message.session_id,)).fetchone()
    if not row or row["customer"] != message.sender:
        return None
    return shipments.for_session(ctx, message.session_id)


async def question(ctx: Context, message: Message) -> str:
    """Subject, body and the text of every attachment: the reference may stand only in an attached enquiry."""
    attached = await extract._attachments(ctx, message) if message.attachments else []
    return "\n".join([message.subject, message.body, *(line for _, lines in attached for line in lines)])


async def handle(ctx: Context, message: Message) -> None:
    """Finds the shipment the question means (a reference, or its description: origin or pickup postcode,
    destination, weight, company) in the CRM and
    answers in the thread from the CRM alone. Says so plainly when we hold no such shipment."""
    found, reference = match(ctx, await question(ctx, message), message.sender)
    if not found and (own := _own_shipment(ctx, message)):
        found = [own]
    body = answer(found, reference, message.body, details(ctx, found, message.body), signer=ctx.settings.signer)
    if (language := extract._language(extract._text(message))) != "en":  # answered in the asker's language
        english = body
        names = tuple(
            n for s in found[:5] for n in (s.id, s.carrier, s.booking_reference, s.origin, s.destination) if n
        )
        body = await compose.localize(ctx, language, message.session_id, body, TRANSLATE_WAIT_S,
                                      lambda text: compose.kept(english, text, names), "tracking.translate")  # fmt: skip
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
