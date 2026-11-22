import json
from decimal import Decimal
from itertools import combinations
from statistics import median

from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, calc, selection, session, store


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
    """Every trucker's priced answers so far by (trucker, zone), oldest first: the pickup price memory. An answer
    counts when it prices the request its session had read (zone, airport, chargeable weight) and its own request
    did not distrust it (`selection.implausible`, round 67)."""
    rows = ctx.db.execute(
        "SELECT o.session_id, o.payload, (SELECT r.payload FROM events r WHERE r.session_id = o.session_id"
        " AND r.kind = 'rfq.extracted' AND r.id < o.id ORDER BY r.id DESC LIMIT 1) AS rfq FROM events o"
        " WHERE o.kind = 'option.found' AND json_extract(o.payload, '$.option.trucker') IS NOT NULL ORDER BY o.id"
    ).fetchall()
    sessions: dict[int, list[tuple[str, RateOption]]] = {}
    for row in rows:
        if not row["rfq"]:
            continue
        asked, answer = Rfq.model_validate(json.loads(row["rfq"])), session.option(json.loads(row["payload"])["option"])
        read = (asked.origin, calc.chargeable_kg(asked.weight_kg, asked.volume_cbm))
        if asked.pickup_zone and read == (answer.origin, answer.chargeable_kg):
            sessions.setdefault(row["session_id"], []).append((asked.pickup_zone, answer))
    memory: dict[tuple[str, str], list[RateOption]] = {}
    for found in sessions.values():
        peers = [answer for _, answer in found]
        for zone, answer in found:
            if not selection.implausible(answer, peers):
                memory.setdefault((answer.trucker, zone), []).append(answer)
    return memory


def _line(answers: list[RateOption]) -> tuple[Decimal, Decimal] | None:
    """A trucker's price in one zone as (base, rate per chargeable kg): the median rate between every two answers
    and the median base it leaves, so one answer at another price level does not move the line. None from one
    weight, or when the answers contradict a price that grows with the weight."""
    points = [(a.chargeable_kg, a.total_eur) for a in answers]
    rates = [(p2 - p1) / (k2 - k1) for (k1, p1), (k2, p2) in combinations(points, 2) if k1 != k2]
    if not rates or (rate := median(rates)) <= 0:
        return None
    return median(price - rate * kg for kg, price in points), rate


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
        lines = {key: line for key, answers in memory.items() if (line := _line(answers))}
        # A trucker with one answer in the zone: that price moved along the shape of the lines known, the fixed
        # charge as the kg it is worth (median over all truckers and zones); without any line, the price as it was.
        fixed_kg = median(base / rate for base, rate in lines.values()) if lines else None
        kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
        estimated = []
        for name, _ in store.carriers(ctx, "trucker", rfq.pickup_zone):
            if not (answers := memory.get((name, rfq.pickup_zone))):
                continue
            last = answers[-1]
            if line := lines.get((name, rfq.pickup_zone)):
                price = line[0] + line[1] * kg
            elif fixed_kg is not None and fixed_kg > 0:
                price = last.total_eur * (fixed_kg + kg) / (fixed_kg + last.chargeable_kg)
            else:
                price = last.total_eur
            if (price := calc.money(price)) <= 0:
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
