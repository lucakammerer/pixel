import json
from datetime import timedelta
from decimal import Decimal
from itertools import takewhile

from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, selection, session, store

MEMORY_H = 24  # the trucker answers the memory reads: a bounded scan of the newest events, whatever the log's size


def zone_of(postcode: str) -> str:
    """Q13 assumption: the pickup zone is the first two digits of the German postcode."""
    return postcode.strip()[:2]


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Door-to-port only: the truckers serving the pickup zone, asked by mail; one option per answer."""
    if not (rfq.door_pickup and rfq.pickup_zone):
        return []
    truckers = store.carriers(ctx, "trucker", rfq.pickup_zone)
    if not truckers:
        return []
    mailed = await bids.request(ctx, rfq, truckers, pickup=True)
    return await bids.collect(ctx, rfq.session_id, deadline, mailed, pickup=True, rfq=rfq)


def _answers(ctx: Context) -> dict[tuple[str, str], list[RateOption]]:
    """Every trucker's priced answers of the last MEMORY_H hours by (trucker, zone), oldest first: the pickup price
    memory. An answer counts when it prices the request its session had read (zone, airport, chargeable weight),
    its own request did not distrust it (`selection.implausible`, round 67) and no repair of its session left it out
    (`option.vetoed`, as `session.options`)."""
    since = (timing.now() - timedelta(hours=MEMORY_H)).isoformat()
    rows = ctx.db.execute(
        "SELECT o.ts, o.session_id, o.kind, o.payload, CASE o.kind WHEN 'option.found' THEN (SELECT r.payload"
        " FROM events r WHERE r.session_id = o.session_id AND r.kind = 'rfq.extracted' AND r.id < o.id"
        " ORDER BY r.id DESC LIMIT 1) END AS rfq FROM events o WHERE o.kind = 'option.vetoed' OR (o.kind ="
        " 'option.found' AND json_extract(o.payload, '$.option.trucker') IS NOT NULL) ORDER BY o.id DESC"
    )  # newest first, read only down to `since`
    sessions: dict[int, list[tuple[str, RateOption]]] = {}
    vetoes: dict[int, list[dict]] = {}
    for row in reversed(list(takewhile(lambda row: row["ts"] >= since, rows))):
        if row["kind"] == "option.vetoed":
            vetoes.setdefault(row["session_id"], []).append(json.loads(row["payload"]))
            continue
        if not row["rfq"]:
            continue
        asked, answer = Rfq.model_validate(json.loads(row["rfq"])), session.option(json.loads(row["payload"])["option"])
        read = (asked.origin, calc.chargeable_kg(asked.weight_kg, asked.volume_cbm))
        if asked.pickup_zone and read == (answer.origin, answer.chargeable_kg):
            sessions.setdefault(row["session_id"], []).append((asked.pickup_zone, answer))
    memory: dict[tuple[str, str], list[RateOption]] = {}
    for session_id, found in sessions.items():
        out = vetoes.get(session_id, [])
        found = [(zone, answer) for zone, answer in found if not any(session.vetoed(answer, v) for v in out)]
        peers = [answer for _, answer in found]
        for zone, answer in found:
            if not selection.implausible(answer, peers):
                memory.setdefault((answer.trucker, zone), []).append(answer)
    return memory


def estimates(ctx: Context, rfq: Rfq, found: list[RateOption]) -> list[RateOption]:
    """A door-to-port request no trucker priced by the time its quote must go (bench/NEXT_CASES.md D5): each of
    the zone's truckers at the price and pickup time its earlier answers give, for `engine.combine` to choose
    from like bids. None beside a real bid in `found`. Never stored as `option.found`:
    `pickup.estimated` says what was estimated, and `feedback.pickup_rule` learns from real answers only.
    Never raises."""
    if not (rfq.door_pickup and rfq.pickup_zone) or any(o.trucker for o in found):
        return []
    try:
        memory = _answers(ctx)
        lines = {
            key: line
            for key, answers in memory.items()
            if (line := calc.pickup_line([(a.chargeable_kg, a.total_eur) for a in answers]))
        }
        fixed_kg = calc.pickup_fixed_kg(list(lines.values()))
        kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
        estimated = []
        for name, _ in store.carriers(ctx, "trucker", rfq.pickup_zone):
            if not (answers := memory.get((name, rfq.pickup_zone))):
                continue
            last = (answers[-1].chargeable_kg, answers[-1].total_eur)
            if (price := calc.pickup_estimate(kg, lines.get((name, rfq.pickup_zone)), last, fixed_kg)) <= 0:
                continue
            hours = next((a.pickup_hours for a in reversed(answers) if a.pickup_hours is not None), None)
            estimated.append(RateOption(
                source=Source.BID, carrier=name, trucker=name, origin=rfq.origin, destination=rfq.destination,
                chargeable_kg=kg, billed_kg=kg, freight=Decimal(0), pickup=price, pickup_hours=hours, total=price,
                currency="EUR", total_eur=price, transit_days=0,
            ))  # fmt: skip
        payload = {"estimated": True, "zone": rfq.pickup_zone, "chargeable_kg": str(kg), "truckers": [
            {"trucker": o.trucker, "pickup": str(o.pickup), "pickup_hours": o.pickup_hours,
             "answers": len(memory[o.trucker, rfq.pickup_zone])} for o in estimated]}  # fmt: skip
        last_said = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'pickup.estimated' ORDER BY id DESC LIMIT 1",
            (rfq.session_id,),
        ).fetchone()
        if estimated and (last_said is None or json.loads(last_said["payload"]) != payload):
            events.emit(ctx, "pickup.estimated", "pricing", session_id=rfq.session_id, payload=payload)
        return estimated
    except Exception as exc:  # noqa: BLE001  the quote goes without the pickup, as before
        events.emit(ctx, "error", "pricing", session_id=rfq.session_id,
                    payload={"where": "pickup.estimates", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip
        return []
