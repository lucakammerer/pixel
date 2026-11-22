"""A copy of the portal's rates that prices a portal airline while the rate API answers 503.

Within a bracket a portal price is the bracket's rate times the weight (no fuel, security or minimum), so one rate
per lane, airline and bracket prices every weight on it. A dataset version changes these rates by one factor for all
(v3 equals v1; v1+ef9a74 is +15 %, v1+a19eb0 +30 %), so the card keeps every portal rate ever read, live or by `run`,
each scaled to the current version by the factor between the two versions on brackets read in both. Rounds 93 to 99
and 120 to 123 were lost for want of it: every one asked for the fastest airline, a portal airline, and the API,
its page and the desk were all silent."""

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.api.portal import NotOnPortal, RateLimited
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

Key = tuple[str, str, str, Decimal]  # origin, destination, airline code, bracket


def breaks(ctx: Context, code: str) -> list[Decimal] | None:
    raw = db.get(ctx.db, f"rate_card.breaks.{code}")
    return [Decimal(b) for b in json.loads(raw)] if raw else None


def _bracket(steps: list[Decimal], kg: Decimal) -> Decimal:
    return max((b for b in steps if b <= kg), default=steps[0])


def _rates(ctx: Context) -> tuple[str | None, dict[Key, dict[str, dict]]]:
    """The newest dataset version and every portal rate read so far by key and version, newest last."""
    rows = ctx.db.execute(
        "SELECT kind, payload FROM events WHERE kind=? OR (kind='option.found'"
        " AND json_extract(payload,'$.option.source')='portal' AND json_extract(payload,'$.option.rate_id') IS NOT NULL"
        " AND json_extract(payload,'$.option.trucker') IS NULL) ORDER BY id",
        (KIND,),
    ).fetchall()
    rates: dict[Key, dict[str, dict]] = {}
    newest = None
    steps: dict[str, list[Decimal] | None] = {}
    for kind, payload in rows:
        p = json.loads(payload)
        if kind == "option.found":
            o = p["option"]
            p = {"origin": o["origin"], "destination": o["destination"], "code": o.get("carrier_code"),
                 "carrier": o["carrier"], "kg": o["billed_kg"], "total": o["total"], "currency": o["currency"],
                 "fx_eur": o.get("fx_eur"), "transit_days": o["transit_days"], "version": o.get("dataset_version")}  # fmt: skip
        if not (p.get("version") and p.get("code")):
            continue
        newest = p["version"]
        if p["code"] not in steps:
            steps[p["code"]] = breaks(ctx, p["code"])
        if not steps[p["code"]]:
            continue
        key = (p["origin"], p["destination"], p["code"], _bracket(steps[p["code"]], Decimal(p["kg"])))
        by_version = rates.setdefault(key, {})
        by_version.pop(p["version"], None)
        by_version[p["version"]] = p
    return newest, rates


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


def option(ctx: Context, rfq: Rfq, code: str) -> RateOption | None:
    """The airline's price from the card at the current version, or None when the card lacks the bracket the
    chargeable weight falls in."""
    steps = breaks(ctx, code)
    if not steps:
        return None
    current, rates = _rates(ctx)
    factors = _factors(current, rates)
    chosen: dict[Decimal, tuple[dict, Decimal]] = {}
    for (origin, destination, c, bracket), by_version in rates.items():
        if (origin, destination, c) != (rfq.origin, rfq.destination, code):
            continue
        usable = [(v, r) for v, r in by_version.items() if v in factors and r["total"]]
        if usable:
            v, read = next(((v, r) for v, r in usable if v == current), usable[-1])
            chosen[bracket] = (read, factors[v])
    kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    own = chosen.get(_bracket(steps, kg))
    if own is None:
        return None
    read = own[0]
    today = rfq.received_at.date()
    tariff = LaneTariff(
        carrier=read["carrier"], origin=rfq.origin, destination=rfq.destination, currency=read["currency"],
        valid_from=today, valid_to=today, version=current, transit_days=read["transit_days"], minimum=Decimal(0),
        breaks=calc.card_breaks([(b, Decimal(r["kg"]), Decimal(r["total"]), f) for b, (r, f) in chosen.items()]),
        fuel_per_kg=Decimal(0), security_per_kg=Decimal(0), source=KIND,
    )  # fmt: skip
    fx = Decimal(str(read["fx_eur"])) if read.get("fx_eur") else None
    priced = calc.price_tariff(tariff, rfq, fx)
    return priced.model_copy(update={"source": Source.PORTAL, "carrier_code": code, "valid_until": None})


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
    """(origin, destination, code, name, bracket) the card lacks, the brackets most requests fall in first. While the
    current version shares no bracket with an older one, a bracket only the older one has comes first: it gives the
    factor that makes all the older rates usable."""
    current, rates = _rates(ctx)
    factors = _factors(current, rates)
    known = {k for k, by_version in rates.items() if any(v in factors for v in by_version)}
    weights = [
        calc.chargeable_kg(Decimal(str(w)), Decimal(str(v or 0)))
        for w, v in ctx.db.execute(
            "SELECT json_extract(payload,'$.weight_kg'), json_extract(payload,'$.volume_cbm') FROM events"
            " WHERE kind='rfq.extracted' AND json_extract(payload,'$.weight_kg') IS NOT NULL"
        ).fetchall()
    ]
    todo, calibrate = [], []
    for lane in await ctx.portal.lanes():
        for code, name in lane.airlines:
            steps = breaks(ctx, code)
            if steps is None:
                try:
                    steps = sorted(Decimal(b) for b in await ctx.portal.brackets(code))
                except NotOnPortal:
                    steps = []
                db.put(ctx.db, f"rate_card.breaks.{code}", json.dumps([str(b) for b in steps]))
            demand = Counter(_bracket(steps, kg) for kg in weights) if steps else Counter()
            for b in steps:
                key = (lane.origin, lane.destination, code, b)
                if key in known:
                    continue
                item = (lane.origin, lane.destination, code, name, b)
                (calibrate if key in rates else todo).append((-demand[b], item))
    if calibrate and len(factors) <= 1:
        return [item for _, item in calibrate[:1]]
    return [item for _, item in sorted(todo, key=lambda t: t[0])]


async def _read(ctx: Context, item: tuple[str, str, str, str, Decimal]) -> None:
    origin, destination, code, name, bracket = item
    kg = _weight(breaks(ctx, code) or [bracket], bracket)
    payload = {"origin": origin, "destination": destination, "code": code, "carrier": name}
    try:
        rate = await ctx.portal.rate(origin, destination, code, kg, Decimal(0), not_after=timing.now())
    except NotOnPortal as exc:
        if type(exc) is not NotOnPortal:  # RatesUnavailable: the API is down, nothing to learn now
            raise
        current, _ = _rates(ctx)
        events.emit(ctx, KIND, "pricing", payload=payload | {"kg": str(kg), "total": None, "version": current})
        return
    events.emit(ctx, KIND, "pricing", payload=payload | {
        "carrier": rate.carrier, "kg": str(rate.chargeable_kg), "total": str(rate.total), "currency": rate.currency,
        "fx_eur": str(rate.fx_eur) if rate.fx_eur else None, "transit_days": rate.transit_days,
        "version": rate.dataset_version})  # fmt: skip


async def run(ctx: Context) -> None:
    """Forever: in quiet time, the minute's rate reads but RESERVE (one a minute until a read names the limit)."""
    recent: list[datetime] = []
    while True:
        await asyncio.sleep(PACE_S)
        now = timing.now()
        recent = [t for t in recent if now - t < timedelta(seconds=60)]
        budget = max(1, ctx.portal.rate_limit - RESERVE) if ctx.portal.rate_limit else 1
        if len(recent) >= budget or not _quiet(ctx):
            continue
        try:
            todo = await _todo(ctx)
            if todo:
                recent.append(now)
                await _read(ctx, todo[0])
        except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError, ValueError, KeyError) as exc:
            events.emit(ctx, "rate_card.paused", "pricing", payload={"error": type(exc).__name__})
            await asyncio.sleep(60)
