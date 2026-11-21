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


def options(ctx: Context, session_id: int) -> list[RateOption]:
    return [RateOption.model_validate(p["option"]) for p in _payloads(ctx, session_id, "option.found")]
