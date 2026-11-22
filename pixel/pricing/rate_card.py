"""A copy of the portal's rates that prices a portal airline while the rate API answers 503.

Within a bracket a portal price is the bracket's rate times the weight (no fuel, security or minimum), so one rate
per lane, airline and bracket prices every weight on it. A dataset version changes these rates by one factor for all
(v3 equals v1; v1+ef9a74 is +15 %, v1+a19eb0 +30 %), so the card keeps every portal rate ever read, live or by `run`,
each scaled to the current version by the factor between the two versions on brackets read in both. A read of a
version that shares no bracket with the current one prices unscaled, and a card whose version no read confirmed for
more than a round says so (`stale`): the version may have changed with the outage, and a sheet price or no price was
never accepted. Rounds 93 to 99 and 120 to 123 were lost for want of it: every one asked for the fastest airline, a
portal airline, and the API, its page and the desk were all silent."""

import asyncio
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal

from pixel.api.portal import NotOnPortal
from pixel.contracts import LaneTariff, RateOption, Rfq, Source
from pixel.core import db, events, timing
from pixel.core.context import Context
from pixel.pricing import calc

ROUND_S = 360  # a request every 6 minutes (R1)
START_S = 90  # a request's own rate reads are done by then, or it has its answer
CLEAR_S = 75  # no read this close to the next request: its rate reads start in a fresh one-minute window
PACE_S = 2
RESERVE = 2  # reads of the minute left to a booking's fresh rate
KIND = "rate_card.read"
DRIFT_PCT = Decimal(1)  # a card this far off the portal's own rate is shown by `feedback.attention`

Key = tuple[str, str, str, Decimal]  # origin, destination, airline code, bracket


def breaks(ctx: Context, code: str) -> list[Decimal] | None:
    raw = db.get(ctx.db, f"rate_card.breaks.{code}")
    return [Decimal(b) for b in json.loads(raw)] if raw else None


def _bracket(steps: list[Decimal], kg: Decimal) -> Decimal:
    return max((b for b in steps if b <= kg), default=steps[0])


def _rates(ctx: Context) -> tuple[str | None, str | None, dict[Key, dict[str, dict]]]:
    """The newest dataset version, when a read last named it, and every portal rate read so far by key and version,
    newest last."""
    rows = ctx.db.execute(
        "SELECT kind, ts, payload FROM events WHERE kind=? OR (kind='option.found'"
        " AND json_extract(payload,'$.option.source')='portal' AND json_extract(payload,'$.option.rate_id') IS NOT NULL"
        " AND json_extract(payload,'$.option.trucker') IS NULL) ORDER BY id",
        (KIND,),
    ).fetchall()
    rates: dict[Key, dict[str, dict]] = {}
    newest = seen = None
    steps: dict[str, list[Decimal] | None] = {}
    for kind, ts, payload in rows:
        p = json.loads(payload)
        if kind == "option.found":
            o = p["option"]
            p = {"origin": o["origin"], "destination": o["destination"], "code": o.get("carrier_code"),
                 "carrier": o["carrier"], "kg": o["billed_kg"], "total": o["total"], "currency": o["currency"],
                 "fx_eur": o.get("fx_eur"), "transit_days": o["transit_days"], "version": o.get("dataset_version")}  # fmt: skip
        if not (p.get("version") and p.get("code")):
            continue
        newest, seen = p["version"], ts
        if p["code"] not in steps:
            steps[p["code"]] = breaks(ctx, p["code"])
        if not steps[p["code"]]:
            continue
        key = (p["origin"], p["destination"], p["code"], _bracket(steps[p["code"]], Decimal(p["kg"])))
        by_version = rates.setdefault(key, {})
        by_version.pop(p["version"], None)
        by_version[p["version"]] = p
    return newest, seen, rates


def _factors(current: str | None, rates: dict[Key, dict[str, dict]]) -> dict[str, Decimal]:
    """Per version, the factor to the current version's rates, from the brackets read in both."""
    pairs: dict[str, list[tuple[Decimal, Decimal, Decimal, Decimal]]] = {}
    for by_version in rates.values():
        now = by_version.get(current)
        if now is None or not now["total"]:
            continue
        for v, then in by_version.items():
            if then["total"]:
                pairs.setdefault(v, []).append(
                    (Decimal(now["total"]), Decimal(now["kg"]), Decimal(then["total"]), Decimal(then["kg"]))
                )
    return {v: calc.version_factor(p) for v, p in pairs.items()}


def _card(ctx: Context, origin: str, destination: str, code: str) -> tuple[dict, dict[Decimal, tuple[dict, Decimal]]]:
    """What the card rests on, and per bracket of the airline on the lane the read that prices it with its factor to
    the current version: the current version's own read, else the newest that can be scaled, else the newest as it
    was read (`unscaled`)."""
    current, seen, rates = _rates(ctx)
    factors = _factors(current, rates)
    chosen, unscaled = {}, set()
    for (o, d, c, bracket), by_version in rates.items():
        if (o, d, c) != (origin, destination, code):
            continue
        reads = [(v, r) for v, r in by_version.items() if r["total"]]
        scaled = [(v, r) for v, r in reads if v in factors]
        if reads:
            v, read = next(((v, r) for v, r in scaled if v == current), (scaled or reads)[-1])
            chosen[bracket] = (read, factors.get(v, Decimal(1)))
            if v not in factors:
                unscaled.add(bracket)
    silent = seen is None or (timing.now() - datetime.fromisoformat(seen)).total_seconds() > ROUND_S + 60
    return {"version": current, "as_of": seen, "silent": silent, "unscaled": unscaled}, chosen


def _note(basis: dict, read: dict, bracket: Decimal) -> dict:
    unscaled = bracket in basis["unscaled"]
    return {"version": basis["version"], "read_in": read["version"], "as_of": basis["as_of"], "unscaled": unscaled,
            "stale": unscaled or basis["silent"]}  # fmt: skip


def price(ctx: Context, origin: str, destination: str, code: str, kg: Decimal) -> tuple[Decimal, dict] | None:
    """The card's total for a billed weight in that weight's own bracket, and what it rests on."""
    steps = breaks(ctx, code)
    if not steps:
        return None
    basis, chosen = _card(ctx, origin, destination, code)
    bracket = _bracket(steps, kg)
    if bracket not in chosen:
        return None
    read, factor = chosen[bracket]
    return calc.card_total(Decimal(read["kg"]), Decimal(read["total"]), factor, kg), _note(basis, read, bracket)


def check(ctx: Context, live: RateOption) -> None:
    """Called with a rate the API just gave, before it joins the card: the card's price for the same lane, airline
    and billed weight and how far it is off (`rate_card.checked`)."""
    if not live.carrier_code or not live.rate_id:
        return
    card = price(ctx, live.origin, live.destination, live.carrier_code, live.billed_kg)
    if card is None or not live.total:
        return
    events.emit(ctx, "rate_card.checked", "pricing", payload={
        "origin": live.origin, "destination": live.destination, "code": live.carrier_code, "kg": str(live.billed_kg),
        "live": str(live.total), "card": str(card[0]), "off_pct": str(calc.off_pct(card[0], live.total)),
        "live_version": live.dataset_version} | card[1])  # fmt: skip


def airlines(ctx: Context, origin: str, destination: str) -> tuple[tuple[str, str], ...]:
    """(code, name) of every airline the card has a rate for on the lane, for a round in which `ports` is down too."""
    _, _, rates = _rates(ctx)
    found = {}
    for (o, d, code, _), by_version in rates.items():
        priced = [r for r in by_version.values() if r["total"]]
        if (o, d) == (origin, destination) and priced:
            found[code] = priced[-1]["carrier"]
    return tuple(found.items())


def option(ctx: Context, rfq: Rfq, code: str) -> tuple[RateOption, dict] | None:
    """The airline's price from the card at the current version and what it rests on, or None when the card lacks
    the bracket the chargeable weight falls in."""
    steps = breaks(ctx, code)
    if not steps:
        return None
    basis, chosen = _card(ctx, rfq.origin, rfq.destination, code)
    kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    own = chosen.get(_bracket(steps, kg))
    if own is None:
        return None
    read = own[0]
    today = rfq.received_at.date()
    tariff = LaneTariff(
        carrier=read["carrier"], origin=rfq.origin, destination=rfq.destination, currency=read["currency"],
        valid_from=today, valid_to=today, version=basis["version"], transit_days=read["transit_days"],
        minimum=Decimal(0),
        breaks=calc.card_breaks([(b, Decimal(r["kg"]), Decimal(r["total"]), f) for b, (r, f) in chosen.items()]),
        fuel_per_kg=Decimal(0), security_per_kg=Decimal(0), source=KIND,
    )  # fmt: skip
    fx = Decimal(str(read["fx_eur"])) if read.get("fx_eur") else None
    priced = calc.price_tariff(tariff, rfq, fx)
    billed = _bracket(steps, priced.billed_kg)
    return (priced.model_copy(update={"source": Source.PORTAL, "carrier_code": code, "valid_until": None}),
            _note(basis, chosen[billed][0], billed))  # fmt: skip


def _quiet(ctx: Context) -> bool:
    """From START_S after the newest request (or its answer) until CLEAR_S before the next; or no request for a
    round."""
    row = ctx.db.execute(
        "SELECT session_id, json_extract(payload,'$.received_at') FROM events WHERE kind='rfq.extracted'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None or row[1] is None:
        return True
    age = (timing.now() - datetime.fromisoformat(row[1])).total_seconds()
    if age >= ROUND_S + 60:
        return True
    if age >= ROUND_S - CLEAR_S:
        return False
    answered = ctx.db.execute(
        "SELECT 1 FROM events WHERE session_id=? AND kind IN ('quote.sent','quote.fallback','quote.missed')", (row[0],)
    ).fetchone()
    return answered is not None or age >= START_S


def _weight(steps: list[Decimal], bracket: Decimal) -> Decimal:
    """Read a bracket at its own break; the bracket from 0 halfway to the next break (a weight of 0 is no read)."""
    if bracket > 0:
        return bracket
    higher = [b for b in steps if b > 0]
    return higher[0] / 2 if higher else Decimal(100)


async def _todo(ctx: Context) -> list[tuple[str, str, str, str, Decimal]]:
    """(origin, destination, code, name, bracket) the card lacks, then those it holds only from an older version
    (scaled, a cent per kg off at most: read again they are exact, and a version that did not change every rate by
    one factor shows in `check`). First the brackets most requests need: their own and the next higher break, which
    is billed when cheaper (15 % of the airlines priced). While the current version shares no bracket with an older
    one, a bracket only the older one has comes first: it gives the factor that makes all the older rates usable."""
    current, _, rates = _rates(ctx)
    factors = _factors(current, rates)
    known = {k for k, by_version in rates.items() if any(v in factors for v in by_version)}
    weights = [
        calc.chargeable_kg(Decimal(str(w)), Decimal(str(v or 0)))
        for w, v in ctx.db.execute(
            "SELECT json_extract(payload,'$.weight_kg'), json_extract(payload,'$.volume_cbm') FROM events"
            " WHERE kind='rfq.extracted' AND json_extract(payload,'$.weight_kg') IS NOT NULL"
        ).fetchall()
    ]
    todo, old, calibrate = [], [], []
    for lane in await ctx.portal.lanes():
        for code, name in lane.airlines:
            steps = breaks(ctx, code)
            if steps is None:
                try:
                    steps = sorted(Decimal(b) for b in await ctx.portal.brackets(code))
                except NotOnPortal:
                    steps = []
                db.put(ctx.db, f"rate_card.breaks.{code}", json.dumps([str(b) for b in steps]))
            demand = Counter()
            for own in (_bracket(steps, kg) for kg in weights) if steps else ():
                demand.update((own, *[b for b in steps if b > own][:1]))
            for b in steps:
                key = (lane.origin, lane.destination, code, b)
                item = (-demand[b], (lane.origin, lane.destination, code, name, b))
                if key not in known:
                    (calibrate if key in rates else todo).append(item)
                elif current not in rates[key]:
                    old.append(item)
    if calibrate and len(factors) <= 1:
        return [item for _, item in calibrate[:1]]
    return [item for _, item in sorted(todo, key=lambda t: t[0]) + sorted(old, key=lambda t: t[0])]


async def _read(ctx: Context, item: tuple[str, str, str, str, Decimal]) -> None:
    origin, destination, code, name, bracket = item
    kg = _weight(breaks(ctx, code) or [bracket], bracket)
    payload = {"origin": origin, "destination": destination, "code": code, "carrier": name}
    try:
        rate = await ctx.portal.rate(origin, destination, code, kg, Decimal(0), not_after=timing.now())
    except NotOnPortal as exc:
        if type(exc) is not NotOnPortal:  # RatesUnavailable: the API is down, nothing to learn now
            raise
        current, _, _ = _rates(ctx)
        events.emit(ctx, KIND, "pricing", payload=payload | {"kg": str(kg), "total": None, "version": current})
        return
    check(ctx, rate)
    events.emit(ctx, KIND, "pricing", payload=payload | {
        "carrier": rate.carrier, "kg": str(rate.chargeable_kg), "total": str(rate.total), "currency": rate.currency,
        "fx_eur": str(rate.fx_eur) if rate.fx_eur else None, "transit_days": rate.transit_days,
        "version": rate.dataset_version})  # fmt: skip


async def run(ctx: Context) -> None:
    """Forever: in quiet time, the minute's rate reads but RESERVE (one a minute until a read names the limit). The
    portal counts reads per clock minute: counted over the last 60 seconds, ten fell into 05:21 and the eleventh got 429."""
    minute, used = None, 0
    while True:
        await asyncio.sleep(PACE_S)
        now = timing.now()
        if now.replace(second=0, microsecond=0) != minute:
            minute, used = now.replace(second=0, microsecond=0), 0
        budget = max(1, ctx.portal.rate_limit - RESERVE) if ctx.portal.rate_limit else 1
        if used >= budget or not _quiet(ctx):
            continue
        try:
            todo = await _todo(ctx)
            if todo:
                used += 1
                await _read(ctx, todo[0])
        except Exception as exc:  # noqa: BLE001  the API, the quota or a read the card cannot take: pause, never stop
            events.emit(ctx, "rate_card.paused", "pricing", payload={"error": type(exc).__name__})
            await asyncio.sleep(60)
