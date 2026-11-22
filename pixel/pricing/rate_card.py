"""A copy of the portal's rates, read in quiet minutes, that prices a portal airline while the rate API answers 503.

Within a bracket a portal price is the bracket's rate times the weight (no fuel, security or minimum), so one read
per lane, airline and bracket of the current dataset version prices every weight on it. Rounds 93 to 99 and 120 to
123 were lost for want of exactly that: every one asked for the fastest airline, a portal airline, and the API, its
page and the desk were all silent. Version v3 held from 22:06 to 04:35, both outages inside it."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.api.portal import NotOnPortal, RateLimited
from pixel.contracts import LaneTariff, RateOption, Rfq, Source
from pixel.core import events, timing
from pixel.core.context import Context
from pixel.pricing import calc

ROUND_S = 360  # a request every 6 minutes (R1)
CLEAR_S = 75  # no read this close to the next request: its rate reads start in a fresh one-minute window
PACE_S = 5
SHARE = 3  # at most a third of the minute's rate reads
KIND = "rate_card.read"


def version(ctx: Context) -> str | None:
    """The newest dataset version any portal rate named; during an outage, the last one seen."""
    row = ctx.db.execute(
        "SELECT COALESCE(json_extract(payload,'$.option.dataset_version'), json_extract(payload,'$.version'))"
        " FROM events WHERE (kind='option.found' AND json_extract(payload,'$.option.source')='portal'"
        " AND json_extract(payload,'$.option.dataset_version') IS NOT NULL)"
        " OR (kind=? AND json_extract(payload,'$.version') IS NOT NULL) ORDER BY id DESC LIMIT 1",
        (KIND,),
    ).fetchone()
    return row[0] if row else None


def _reads(ctx: Context, at: str | None, **where: str) -> list[dict]:
    filters = "".join(f" AND json_extract(payload,'$.{key}')=?" for key in where)
    sql = f"SELECT payload FROM events WHERE kind=? AND json_extract(payload,'$.version')=?{filters} ORDER BY id"
    return [json.loads(r[0]) for r in ctx.db.execute(sql, (KIND, at, *where.values())).fetchall()]


def option(ctx: Context, rfq: Rfq, code: str) -> RateOption | None:
    """The airline's price from the card for the current version, or None when the card lacks the bracket the
    chargeable weight falls in."""
    at = version(ctx)
    reads = [r for r in _reads(ctx, at, origin=rfq.origin, destination=rfq.destination, code=code) if r["total"]]
    if not reads:
        return None
    newest = {Decimal(r["bracket"]): r for r in reads}
    last = reads[-1]
    breaks = [Decimal(b) for b in last["breaks"]]
    kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    if max((b for b in breaks if b <= kg), default=breaks[0]) not in newest:
        return None
    today = rfq.received_at.date()
    tariff = LaneTariff(
        carrier=last["carrier"], origin=rfq.origin, destination=rfq.destination, currency=last["currency"],
        valid_from=today, valid_to=today, version=at, transit_days=last["transit_days"], minimum=Decimal(0),
        breaks=calc.card_breaks([(b, Decimal(r["kg"]), Decimal(r["total"])) for b, r in newest.items()]),
        fuel_per_kg=Decimal(0), security_per_kg=Decimal(0), source=KIND,
    )  # fmt: skip
    fx = Decimal(last["fx_eur"]) if last.get("fx_eur") else None
    priced = calc.price_tariff(tariff, rfq, fx)
    return priced.model_copy(update={"source": Source.PORTAL, "carrier_code": code, "valid_until": None})


def _quiet(ctx: Context) -> bool:
    """The newest request has its answer and the next is more than CLEAR_S away, or no request came for a round."""
    row = ctx.db.execute(
        "SELECT session_id, json_extract(payload,'$.received_at') FROM events WHERE kind='rfq.extracted'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None or row[1] is None:
        return True
    age = (timing.now() - datetime.fromisoformat(row[1])).total_seconds()
    if age >= ROUND_S + 60:
        return True
    answered = ctx.db.execute(
        "SELECT 1 FROM events WHERE session_id=? AND kind IN ('quote.sent','quote.fallback','quote.missed')", (row[0],)
    ).fetchone()
    return answered is not None and age < ROUND_S - CLEAR_S


def _weight(breaks: list[Decimal], i: int) -> Decimal:
    """Read a bracket at its own break; the bracket from 0 halfway to the next break (a weight of 0 is no read)."""
    if breaks[i] > 0:
        return breaks[i]
    return breaks[i + 1] / 2 if i + 1 < len(breaks) else Decimal(100)


async def _todo(ctx: Context, breaks: dict[tuple[str, str | None], list[Decimal]]) -> list[tuple]:
    """(origin, destination, code, name, bracket) the card lacks for the current version; the brackets near 1000 kg,
    where most requests fall, first on every lane."""
    at = version(ctx)
    done = {(r["origin"], r["destination"], r["code"], Decimal(r["bracket"])) for r in _reads(ctx, at)}
    todo = []
    for lane in await ctx.portal.lanes():
        for code, name in lane.airlines:
            if (code, at) not in breaks:
                try:
                    breaks[(code, at)] = sorted(Decimal(b) for b in await ctx.portal.brackets(code))
                except NotOnPortal:
                    breaks[(code, at)] = []
            todo += [(lane.origin, lane.destination, code, name, b) for b in breaks[(code, at)]
                     if (lane.origin, lane.destination, code, b) not in done]  # fmt: skip
    return sorted(todo, key=lambda t: abs(t[4] - 1000))


async def _read(ctx: Context, item: tuple, breaks: list[Decimal]) -> None:
    origin, destination, code, name, bracket = item
    kg = _weight(breaks, breaks.index(bracket))
    payload = {"origin": origin, "destination": destination, "code": code, "carrier": name,
               "bracket": str(bracket), "kg": str(kg), "breaks": [str(b) for b in breaks]}  # fmt: skip
    try:
        rate = await ctx.portal.rate(origin, destination, code, kg, Decimal(0), not_after=timing.now())
    except NotOnPortal as exc:
        if type(exc) is not NotOnPortal:  # RatesUnavailable: the API is down, nothing to learn now
            raise
        events.emit(ctx, KIND, "pricing", payload=payload | {"total": None, "version": version(ctx)})
        return
    events.emit(ctx, KIND, "pricing", payload=payload | {
        "carrier": rate.carrier, "kg": str(rate.chargeable_kg), "total": str(rate.total), "currency": rate.currency,
        "fx_eur": str(rate.fx_eur) if rate.fx_eur else None, "transit_days": rate.transit_days,
        "version": rate.dataset_version})  # fmt: skip


async def run(ctx: Context) -> None:
    """Forever: in a quiet minute, one rate read every PACE_S, at most a SHARE of the minute's limit."""
    breaks: dict[tuple[str, str | None], list[Decimal]] = {}
    recent: list = []
    while True:
        await asyncio.sleep(PACE_S)
        now = timing.now()
        recent = [t for t in recent if now - t < timedelta(seconds=60)]
        if len(recent) >= max(1, (ctx.portal.rate_limit or SHARE) // SHARE) or not _quiet(ctx):
            continue
        try:
            todo = await _todo(ctx, breaks)
            if todo:
                recent.append(now)
                await _read(ctx, todo[0], breaks[(todo[0][2], version(ctx))])
        except (NotOnPortal, RateLimited, ApiError, httpx.HTTPError, ValueError, KeyError) as exc:
            events.emit(ctx, "rate_card.paused", "pricing", payload={"error": type(exc).__name__})
            await asyncio.sleep(60)
