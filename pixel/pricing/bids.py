"""Bid requests to carriers by mail and their answers as RateOptions. Rules read the answer; the model only
transcribes an answer the rules cannot read, and code does every sum."""

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

from pixel import llm
from pixel.contracts import Message, RateOption, Rfq, Source
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import calc, session, store

POLL_S = 0.5
AMOUNT = r"\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}
PRICED = re.compile(
    rf"(?P<c1>[A-Z]{{3}}|[€$£])\s?(?P<n1>{AMOUNT})|(?P<n2>{AMOUNT})\s?(?P<c2>[A-Z]{{3}}|[€$£])(?![A-Za-z])"
)
TOTAL_WORDS = re.compile(r"total|all[- ]in|gesamt|in total|overall", re.IGNORECASE)
TRANSIT = re.compile(r"(\d+)\s*(?:calendar\s+)?(?:days?|tage?n?)\b|transit\D{0,20}(\d+)", re.IGNORECASE)
FX = re.compile(r"1\s*([A-Z]{3})\s*=\s*(\d+[.,]\d+)\s*EUR|(\d+[.,]\d+)\s*EUR\s*(?:per|/)\s*(?:1\s*)?([A-Z]{3})")
DECLINE = re.compile(
    r"\b(?:cannot offer|can't offer|unable to|no capacity|not able to|decline|regret)\b", re.IGNORECASE
)


def _amount(text: str) -> Decimal:
    if not (m := re.search(AMOUNT, text)):
        raise ValueError(f"no amount in {text!r}")
    text = m.group().replace(" ", "")
    if "." in text and "," in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        text = text.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        head, _, tail = text.rpartition(sep)
        text = head.replace(sep, "") + ("." + tail if len(tail) != 3 else tail)
    return Decimal(text)


def _own_text(body: str) -> str:
    """The carrier's own words: quoted lines and our request below are dropped."""
    lines = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^\s*(?:on .+ wrote:|-----\s*original message)", line, re.IGNORECASE):
            break
        lines.append(line)
    return "\n".join(lines)


def _price(text: str) -> tuple[Decimal, str] | None:
    found = []
    for line in FX.sub("", text).splitlines():
        for m in PRICED.finditer(line):
            currency = m.group("c1") or m.group("c2")
            found.append(
                (
                    bool(TOTAL_WORDS.search(line)),
                    _amount(m.group("n1") or m.group("n2")),
                    SYMBOLS.get(currency, currency),
                )
            )
    totals = [(amount, cur) for is_total, amount, cur in found if is_total]
    if totals:
        return totals[-1]
    distinct = {(amount, cur) for _, amount, cur in found}
    return distinct.pop() if len(distinct) == 1 else None


def _fx(text: str, currency: str) -> Decimal | None:
    for m in FX.finditer(text):
        code, rate = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if code == currency:
            return _amount(rate)
    return None


def _option(rfq: Rfq, message: Message, total: Decimal, currency: str, fx: Decimal | None, days: int) -> RateOption:
    chargeable = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    total = calc.money(total)
    return RateOption(
        source=Source.BID,
        carrier=message.sender,
        origin=rfq.origin,
        destination=rfq.destination,
        chargeable_kg=chargeable,
        billed_kg=chargeable,
        freight=total,
        total=total,
        currency=currency,
        fx_eur=None if currency == "EUR" else fx,
        total_eur=calc.to_eur(total, currency, fx),
        transit_days=days,
        evidence_message_id=message.id,
    )


def parse(message: Message, rfq: Rfq, *, needs_transit: bool = True) -> RateOption | None:
    """Price, currency and transit time from a carrier's answer, by rules. None when the rules cannot read it."""
    text = _own_text(message.body)
    if DECLINE.search(text) or not (price := _price(text)):
        return None
    total, currency = price
    fx = None if currency == "EUR" else _fx(text, currency)
    transit = TRANSIT.search(text)
    if (currency != "EUR" and fx is None) or (needs_transit and not transit):
        return None
    days = int(transit.group(1) or transit.group(2)) if transit else 0
    return _option(rfq, message, total, currency, fx, days)


ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["offer", "total", "components", "currency", "transit_days", "eur_per_unit"],
    "properties": {
        "offer": {"type": "boolean", "description": "false when the carrier declines or gives no price"},
        "total": {"type": ["string", "null"], "description": "the all-in total exactly as written, if stated"},
        "components": {"type": "array", "items": {"type": "string"}, "description": "each charge as written"},
        "currency": {"type": ["string", "null"]},
        "transit_days": {"type": ["string", "null"]},
        "eur_per_unit": {"type": ["string", "null"], "description": "exchange rate to EUR, only if stated"},
    },
}

ANSWER_PROMPT = (
    "A carrier answered our request for a freight price. Copy what the mail states, numbers exactly as written; "
    "never compute, convert or guess. If only separate charges are given, list them in components and leave total "
    "null."
)


async def _read_with_model(ctx: Context, message: Message, rfq: Rfq, needs_transit: bool) -> RateOption | None:
    result = await llm.ask_json(
        ctx,
        purpose="bid.read",
        system=ANSWER_PROMPT,
        user=_own_text(message.body)[:4000],
        schema=ANSWER_SCHEMA,
        session_id=message.session_id,
        max_output_tokens=150,
    )
    data = result.data
    if not data["offer"] or not data["currency"]:
        return None
    total = _amount(data["total"]) if data["total"] else sum((_amount(c) for c in data["components"]), Decimal(0))
    currency = SYMBOLS.get(data["currency"].strip(), data["currency"].strip().upper())
    fx = _amount(data["eur_per_unit"]) if data["eur_per_unit"] else None
    transit = re.search(r"\d+", data["transit_days"] or "")
    if total <= 0 or (currency != "EUR" and fx is None) or (needs_transit and not transit):
        return None
    return _option(rfq, message, total, currency, fx, int(transit.group()) if transit else 0)


def _subject(rfq: Rfq, pickup: bool) -> str:
    if pickup:
        return f"Pickup request {rfq.pickup_postcode or rfq.pickup_zone} to {rfq.origin}"
    return f"Rate request {rfq.origin}-{rfq.destination}, {rfq.weight_kg} kg"


def _body(rfq: Rfq, pickup: bool) -> str:
    chargeable = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    shipment = f"{rfq.weight_kg} kg gross, {rfq.volume_cbm} cbm" + (f", {rfq.pieces} pieces" if rfq.pieces else "")
    if pickup:
        return (
            "Hello,\n\nplease quote a pickup for an air export shipment:\n"
            f"From: postcode {rfq.pickup_postcode or '-'} (zone {rfq.pickup_zone})\nTo: {rfq.origin} airport\n"
            f"Shipment: {shipment}\n\nPlease reply with your price and currency.\n\nThank you,\nPIXEL Air Freight"
        )
    return (
        "Hello,\n\nplease quote air freight for this shipment:\n"
        f"From: {rfq.origin}\nTo: {rfq.destination}\nShipment: {shipment}, chargeable weight {chargeable} kg\n\n"
        "Please reply with your all-in price (freight, fuel and security) with currency, and the transit time in "
        "days.\n\nThank you,\nPIXEL Air Freight"
    )


async def request(ctx: Context, rfq: Rfq, carriers: list[tuple[str, str]], *, pickup: bool = False) -> list[int]:
    """One bid request per carrier (airline, trucker or the rate desk). Returns the sent mail ids; the poller
    maps the answers back to the session through them."""

    async def one(name: str, email: str) -> int | None:
        try:
            sent = await outbox.send(
                ctx,
                to=email,
                subject=_subject(rfq, pickup),
                body=_body(rfq, pickup),
                session_id=rfq.session_id,
                purpose="pickup.request" if pickup else "bid.request",
            )
        except outbox.NotSent:
            return None
        events.emit(
            ctx,
            "bid.requested",
            "pricing",
            session_id=rfq.session_id,
            message_id=sent.id,
            payload={"carrier": name, "email": email, "pickup": pickup},
        )
        return sent.id

    sent = await asyncio.gather(*(one(name, email) for name, email in carriers))
    return [message_id for message_id in sent if message_id is not None]


async def record(ctx: Context, message: Message) -> None:
    """Called by the orchestrator for every bid or desk answer: parse (model only if `parse` gives None),
    emit `bid.received` and `option.found`."""
    rfq = session.rfq(ctx, message.session_id)
    if rfq is None:
        events.emit(ctx, "error", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"error": "bid for a session without an RFQ"})  # fmt: skip
        return
    name, kind = store.carrier_by_email(ctx, message.sender) or (message.sender, "ratedesk")
    pickup = kind == "trucker"
    option, read_by = parse(message, rfq, needs_transit=not pickup), "rules"
    if option is None and not DECLINE.search(_own_text(message.body)):
        read_by = "model"
        try:
            option = await _read_with_model(ctx, message, rfq, needs_transit=not pickup)
        except Exception as exc:  # noqa: BLE001  one unreadable answer must not stop the others
            events.emit(ctx, "error", "pricing", session_id=message.session_id, message_id=message.id,
                        payload={"where": "bid.read", "error": type(exc).__name__})  # fmt: skip
    events.emit(
        ctx,
        "bid.received",
        "pricing",
        session_id=message.session_id,
        message_id=message.id,
        payload={"carrier": name, "pickup": pickup, "priced": option is not None, "read_by": read_by},
    )
    if option is None:
        return
    if pickup:
        option = option.model_copy(update={"freight": Decimal(0), "pickup": option.total, "trucker": name})
    elif kind == "ratedesk":
        option = option.model_copy(update={"source": Source.RATEDESK})
    option = option.model_copy(update={"carrier": name})
    events.emit(ctx, "option.found", "pricing", session_id=message.session_id, message_id=message.id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip


def _answers(ctx: Context, session_id: int, pickup: bool) -> int:
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'bid.received'", (session_id,)
    ).fetchall()
    return sum(1 for row in rows if json.loads(row["payload"]).get("pickup") == pickup)


async def collect(
    ctx: Context, session_id: int, deadline: Deadline, expected: int, *, pickup: bool = False
) -> list[RateOption]:
    """Waits until all expected bids are in or the deadline's target, whichever comes first."""
    while _answers(ctx, session_id, pickup) < expected and datetime.now(UTC) < deadline.target_at:
        await asyncio.sleep(POLL_S)
    return [
        o
        for o in session.options(ctx, session_id)
        if o.source in (Source.BID, Source.RATEDESK) and (o.trucker is not None) == pickup
    ]
