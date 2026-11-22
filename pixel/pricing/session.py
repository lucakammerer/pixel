"""What a session already knows, read back from the event log: its RFQ and the options found so far."""

import json

from pixel.contracts import RateOption, Rfq, Source
from pixel.core.context import Context
from pixel.pricing import calc, store


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
    """Every option found, without those a repair left out (`option.vetoed`, `agents/rfq/repair.py`)."""
    found = [option(p["option"]) for p in _payloads(ctx, session_id, "option.found")]
    out = _payloads(ctx, session_id, "option.vetoed")
    return [o for o in found if not any(vetoed(o, v) for v in out)] if out else found


def veto(option: RateOption, trucker: bool) -> dict:
    """The record of a left-out option: its trucker, or its airline (with the source, so a bid is left out and the
    same airline's sheet or portal rate stays)."""
    if trucker:
        return {"kind": "trucker", "name": option.trucker}
    return {"kind": "airline", "name": option.carrier, "source": option.source.value}


def vetoed(option: RateOption, record: dict) -> bool:
    """Whether `option`, a found one or one with its pickup added, is built from what `record` left out."""
    if record.get("kind") == "trucker":
        return option.trucker is not None and option.trucker == record.get("name")
    if record.get("kind") != "airline" or option.carrier != record.get("name"):
        return False
    return record.get("source") is None or option.source.value == record["source"]


def on_lane(option: RateOption, rfq: Rfq) -> bool:
    return (option.origin, option.destination) == (rfq.origin, rfq.destination)


def for_request(ctx: Context, rfq: Rfq) -> list[RateOption]:
    """Reuse stored prices for the current lane and exclude sheet versions outside a future ready date."""
    found = [option for option in options(ctx, rfq.session_id) if on_lane(option, rfq)]
    ready = calc.ready_day(rfq)
    if ready <= rfq.received_at.date() or not any(option.source == Source.SHEET for option in found):
        return found
    versions = {(tariff.carrier, tariff.version) for tariff in store.lookup(ctx, rfq.origin, rfq.destination, ready)}
    return [
        option for option in found
        if option.source != Source.SHEET or (option.carrier, option.dataset_version) in versions
    ]  # fmt: skip
