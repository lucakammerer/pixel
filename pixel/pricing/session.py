"""What a session already knows, read back from the event log: its RFQ and the options found so far."""

import json

from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context


def _payloads(ctx: Context, session_id: int, kind: str) -> list[dict]:
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = ? ORDER BY id", (session_id, kind)
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def rfq(ctx: Context, session_id: int) -> Rfq | None:
    payloads = _payloads(ctx, session_id, "rfq.extracted")
    return Rfq.model_validate(payloads[-1]) if payloads else None


def option(stored: dict) -> RateOption:
    """An option from the log; fields a hand-deployed branch wrote and main lacks are dropped."""
    return RateOption.model_validate({k: v for k, v in stored.items() if k in RateOption.model_fields})


def options(ctx: Context, session_id: int) -> list[RateOption]:
    return [option(p["option"]) for p in _payloads(ctx, session_id, "option.found")]


def on_lane(option: RateOption, rfq: Rfq) -> bool:
    return (option.origin, option.destination) == (rfq.origin, rfq.destination)


def for_request(ctx: Context, rfq: Rfq) -> list[RateOption]:
    """A previous extraction in this session can have priced different airports."""
    return [o for o in options(ctx, rfq.session_id) if on_lane(o, rfq)]
