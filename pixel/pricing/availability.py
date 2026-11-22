"""Dated notices about new bookings on a specific airline lane."""

import re
from datetime import date

from pixel.contracts import Rfq
from pixel.core import timing
from pixel.core.context import Context

NOTICE = re.compile(
    r"^\s*new bookings\s+(?:on|for)\s+(?P<origin>(?-i:[A-Z]{3}))\s*(?:-|–|to)\s*"
    r"(?P<destination>(?-i:[A-Z]{3}))\s+(?:are\s+)?(?P<state>suspended|withdrawn|resumed|reopened)\s+"
    r"(?:effective(?:\s+from)?|from|on)\s+(?P<effective>\d{4}-\d{2}-\d{2})(?!\s+\d{1,2}:\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)


def notice(text: str) -> dict[str, str] | None:
    """Only an explicit lane, status and effective date can change availability."""
    from pixel.agents.rfq.extract import HISTORY

    match = NOTICE.search(HISTORY.split(text, maxsplit=1)[0])
    if not match:
        return None
    data = match.groupdict()
    try:
        date.fromisoformat(data["effective"])
    except ValueError:
        return None
    data["state"] = data["state"].lower()
    return data


def suspended_carriers(ctx: Context, rfq: Rfq) -> set[str]:
    rows = ctx.db.execute(
        "SELECT json_extract(payload, '$.carrier'), json_extract(payload, '$.state') FROM events"
        " WHERE kind='carrier.availability' AND json_extract(payload, '$.origin')=?"
        " AND json_extract(payload, '$.destination')=? AND json_extract(payload, '$.effective')<=?"
        " ORDER BY json_extract(payload, '$.effective') DESC, message_id DESC, id DESC",
        (rfq.origin, rfq.destination, timing.now().date().isoformat()),
    )
    latest = {}
    for carrier, state in rows:
        latest.setdefault(carrier.casefold(), state)
    return {carrier for carrier, state in latest.items() if state in {"suspended", "withdrawn"}}
