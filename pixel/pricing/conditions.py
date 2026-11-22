"""Explicit carrier terms and pickup requirements that can affect a quote."""

import json
import re
from datetime import date, time, timedelta
from decimal import Decimal

from pixel.contracts import Rfq
from pixel.core.context import Context

LANE = r"(?P<origin>(?-i:[A-Z]{3}))\s*(?:-|–|to)\s*(?P<destination>(?-i:[A-Z]{3}))"
ON = r"(?:effective(?:\s+from)?|from|on)\s+(?P<effective>\d{4}-\d{2}-\d{2}|midnight)(?!\s+\d{1,2}:\d{2})"
LANE_NOTICE = re.compile(
    rf"^\s*(?:new bookings\s+(?:on|for)\s+{LANE}\s+(?:are\s+)?"
    rf"(?P<state>suspended|resumed|withdrawn|reopened)\s+{ON})\b",
    re.IGNORECASE | re.MULTILINE,
)
AWB_DATE = r"(?:air\s*waybill|AWB)\s+(?:issue|issuance|execution)\s+date"
FUEL_NOTICE = re.compile(
    rf"^\s*(?:new\s+)?(?:fuel surcharge|FSC)\s*:?\s*(?P<currency>(?-i:[A-Z]{{3}}))\s+"
    rf"(?P<amount>\d+(?:[.,]\d+)?)\s*(?:/|per\s+)\s*kg\s*(?:chargeable weight\s*)?[,;]?\s*"
    rf"{ON}\s*[,;]?\s*(?:based on|by)\s+(?:the\s+)?{AWB_DATE}\b",
    re.IGNORECASE | re.MULTILINE,
)
WAYBILL_DAY = re.compile(rf"\b{AWB_DATE}\s*(?::|is|will be)\s*(\d{{4}}-\d{{2}}-\d{{2}})\b", re.IGNORECASE)
LIFTGATE = re.compile(r"\b(?:lift[- ]?gate|tail[- ]?lift|hebebühne)\b", re.IGNORECASE)
NO_DOCK = re.compile(r"\bno (?:loading )?dock\b", re.IGNORECASE)
NO_LIFTGATE = re.compile(
    r"\b(?:without|no)\s+(?:a\s+)?(?:lift[- ]?gate|tail[- ]?lift|hebebühne)\b|"
    r"\b(?:lift[- ]?gate|tail[- ]?lift|hebebühne)\s+(?:unavailable|not (?:included|available))\b",
    re.IGNORECASE,
)
CUTOFF = re.compile(r"\b(?:dock|site|warehouse) closes at\s+([01]?\d|2[0-3]):([0-5]\d)\b", re.IGNORECASE)
PICKUP_AT = re.compile(r"\b(?:pickup|collect(?:ion)?)\s+at\s+([01]?\d|2[0-3]):([0-5]\d)\b", re.IGNORECASE)
AS_REQUESTED = re.compile(
    r"\b(?:we (?:can|will) (?:collect|pick up)|pickup (?:available|confirmed))\s+as requested\b", re.IGNORECASE
)
BEFORE_CLOSE = re.compile(r"\bbefore (?:the )?(?:dock|site|warehouse) closes\b", re.IGNORECASE)


def notice(text: str, received: date) -> dict[str, str] | None:
    """Recognize a single, explicit dated lane update or replacement fuel surcharge from a carrier."""
    match = LANE_NOTICE.search(text) or FUEL_NOTICE.search(text)
    if not match:
        return None
    data = match.groupdict()
    try:
        effective = (
            received + timedelta(days=1)
            if data["effective"].lower() == "midnight"
            else date.fromisoformat(data["effective"])
        )
    except ValueError:
        return None
    data["effective"] = effective.isoformat()
    return data


def _notices(ctx: Context, carrier: str) -> list[dict]:
    rows = ctx.db.execute(
        "SELECT payload, message_id FROM events WHERE kind = 'tariff.notice' AND lower(json_extract(payload, '$.carrier')) = ?"
        " ORDER BY json_extract(payload, '$.effective') DESC, message_id DESC, id DESC",
        (carrier.casefold(),),
    )
    return [{**json.loads(row[0]), "message_id": row[1]} for row in rows]


def lane_suspended(ctx: Context, carrier: str, rfq: Rfq, on: date) -> bool:
    active = next((item for item in _notices(ctx, carrier)
                   if item.get("origin") == rfq.origin and item.get("destination") == rfq.destination
                   and item["effective"] <= on.isoformat()), None)  # fmt: skip
    return bool(active and active["state"].casefold() in {"suspended", "withdrawn"})


def request_text(ctx: Context, rfq: Rfq) -> str:
    from pixel.agents.rfq.extract import HISTORY

    row = ctx.db.execute("SELECT subject, body FROM messages WHERE id = ?", (rfq.message_id,)).fetchone()
    return HISTORY.split("\n".join(row), maxsplit=1)[0] if row else ""


def fuel_revision(ctx: Context, carrier: str, currency: str, rfq: Rfq) -> tuple[Decimal | None, int | None]:
    bulletins = [item for item in _notices(ctx, carrier) if item.get("currency") == currency]
    if not bulletins:
        return None, None
    issue = WAYBILL_DAY.search(request_text(ctx, rfq))
    if issue:
        on = date.fromisoformat(issue[1]).isoformat()
    else:
        on = rfq.received_at.date().isoformat()
        if any(on < item["effective"] for item in bulletins):
            raise ValueError("waybill issue date required to price the fuel surcharge")
    applicable = next((item for item in bulletins if item["effective"] <= on), None)
    if not applicable:
        return None, None
    return Decimal(applicable["amount"].replace(",", ".")), applicable["message_id"]


def pickup_requirements(ctx: Context, rfq: Rfq) -> tuple[bool, time | None]:
    text = request_text(ctx, rfq)
    cutoff = CUTOFF.search(text)
    liftgate = (LIFTGATE.search(text) or NO_DOCK.search(text)) and not NO_LIFTGATE.search(text)
    return bool(liftgate), time(int(cutoff[1]), int(cutoff[2])) if cutoff else None


def pickup_terms(requirements: tuple[bool, time | None]) -> dict:
    liftgate, cutoff = requirements
    return {"liftgate": liftgate, "cutoff": cutoff.isoformat() if cutoff else None}


def pickup_fits(requirements: tuple[bool, time | None], reply: str) -> bool:
    liftgate, cutoff = requirements
    agreed = bool(AS_REQUESTED.search(reply))
    if liftgate and (NO_LIFTGATE.search(reply) or not (agreed or LIFTGATE.search(reply))):
        return False
    pickup = PICKUP_AT.search(reply)
    if cutoff and pickup:
        return time(int(pickup[1]), int(pickup[2])) < cutoff
    return cutoff is None or agreed or bool(BEFORE_CLOSE.search(reply))
