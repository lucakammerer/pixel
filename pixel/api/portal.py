"""GET /v1/portal/ports, /brackets, /rates, POST and DELETE /v1/portal/bookings."""

import asyncio
import base64
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.contracts import RateOption, Source
from pixel.core import timing
from pixel.pricing.calc import to_eur


@dataclass(frozen=True)
class Lane:
    origin: str
    destination: str
    airlines: tuple[tuple[str, str], ...]  # (code, name)


@dataclass(frozen=True)
class PortalBooking:
    booking_reference: str
    team_reference: str
    rate_id: str
    airline: str
    price: Decimal
    currency: str
    status: str


class NotOnPortal(Exception):
    """404 from /brackets or 503 from /rates: `hint` is the API's message on where to get the rate instead."""

    def __init__(self, hint: str):
        super().__init__(hint)
        self.hint = hint


class RatesUnavailable(NotOnPortal):
    """503 from /rates: the rate API is down for every airline, not this airline off the portal (rounds 27 to 29).
    `page`: why the browser page gave no rate either, its status and first words (round 120: 403 "Just a moment...")."""

    def __init__(self, hint: str, page: str | None = None):
        super().__init__(hint)
        self.page = page


def _refusal(response: httpx.Response) -> str:
    words = " ".join(re.sub(r"<[^>]*>", " ", response.text).split())
    return f"{response.status_code} {words[:80]}"


class RateExpired(Exception):
    """410 from /bookings."""


class BookingUnavailable(Exception):
    """503 from /bookings: `hint` says how to book by email."""

    def __init__(self, hint: str):
        super().__init__(hint)
        self.hint = hint


class RateLimited(Exception):
    """429 that the caller's deadline does not allow us to wait out."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited for {retry_after:g} s")
        self.retry_after = retry_after


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            if body.get(key):
                return str(body[key])[:500]
    return str(body)[:500]


def _now() -> datetime:
    return timing.now()


PAGE_TRIES = 3  # a sum we cannot read or the page refuses costs one fresh page load
GLYPH = re.compile(r"<g\b[^>]*>(.*?)</g>", re.DOTALL)
STROKE = re.compile(r'<line x1="(-?[\d.]+)" y1="(-?[\d.]+)" x2="(-?[\d.]+)" y2="(-?[\d.]+)"')
DIGITS = {"abcdef": "0", "bc": "1", "abdeg": "2", "abcdg": "3", "bcfg": "4", "acdfg": "5", "acdefg": "6",
          "abc": "7", "abcdefg": "8", "abcdfg": "9"}  # fmt: skip
SUM = re.compile(r"(\d+)([+-])(\d+)=")
ROW = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.DOTALL)
TAG = re.compile(r"<[^>]+>")
AIRLINE = re.compile(r"(.+?)\s*\((\w+)\)")


def _glyph(strokes: list[tuple[float, ...]]) -> str:
    """One sign of the page's sum: a seven-segment digit, or "-" (one bar), "=" (two bars), "+" (a cross). The
    strokes are jittered by a few units in a box about 22 wide and 42 high; "?" for anything else."""
    across = [(y1 + y2) / 2 for x1, y1, x2, y2 in strokes if abs(y1 - y2) <= 6 < abs(x1 - x2)]
    down = [((x1 + x2) / 2, (y1 + y2) / 2) for x1, y1, x2, y2 in strokes if abs(x1 - x2) <= 6 < abs(y1 - y2)]
    if len(across) + len(down) != len(strokes):
        return "?"
    if not down and len(across) in (1, 2):
        return "-" if len(across) == 1 else "="
    if len(across) == 1 and len(down) == 1 and 6 < down[0][0] < 17:
        return "+"
    segments = {"a" if y < 8 else "g" if y < 30 else "d" for y in across}
    segments |= {("f" if y < 20.5 else "e") if x < 11 else ("b" if y < 20.5 else "c") for x, y in down}
    return DIGITS.get("".join(sorted(segments)), "?")


def solve_sum(svg: str) -> int | None:
    """The answer to the sum the portal page draws, read from its strokes by rule; None when a sign is unknown."""
    signs = "".join(_glyph([tuple(map(float, m)) for m in STROKE.findall(g)]) for g in GLYPH.findall(svg))
    if not (match := SUM.fullmatch(signs)):
        return None
    return int(match[1]) + int(match[3]) if match[2] == "+" else int(match[1]) - int(match[3])


def page_rate(html: str) -> dict:
    """The page's rate table as the API's rate. The page mixes hidden decoy characters into the rate id and the
    total: only spans of a class its stylesheet shows are read, any other styling raises ValueError."""
    hidden = set(re.findall(r"\.([\w-]+)\{display:none\}", html))
    shown = set(re.findall(r"\.([\w-]+)\{display:inline\}", html))
    if set(re.findall(r'<span class="([^"]*)"', html)) - hidden - shown:
        raise ValueError("unknown styling on the portal page")
    for cls in hidden:
        html = re.sub(rf'<span class="{re.escape(cls)}">[^<]*</span>', "", html)
    rows = {TAG.sub("", k).strip().lower(): TAG.sub("", v).strip() for k, v in ROW.findall(html)}
    airline = AIRLINE.fullmatch(rows["airline"])
    origin, destination = rows["lane"].split("-")
    return {
        "rate_id": rows["rate_id"],
        "airline": airline[1],
        "airline_code": airline[2],
        "origin": origin,
        "destination": destination,
        "chargeable_weight_kg": rows["chargeable weight (kg)"],
        "total": rows["total"],
        "currency": rows["currency"],
        "transit_days": rows["transit days"],
        "valid_until": rows["valid until"],
    }


class Portal:
    def __init__(self, client: httpx.AsyncClient, page_url: str = ""):
        self.client = client
        self.page_url = page_url  # the portal's browser page, read when the rate API answers 503; empty: never
        self.rate_limit: int | None = None  # X-RateLimit-Limit
        self.rate_remaining: int | None = None  # X-RateLimit-Remaining
        self.window_reset_at: datetime | None = None  # from the last 429's Retry-After

    def _read_limits(self, response: httpx.Response) -> None:
        """Read each quota hint independently, keeping the last usable value if a header is missing or invalid."""
        if (limit := response.headers.get("X-RateLimit-Limit")) is not None:
            with suppress(ValueError):
                self.rate_limit = int(limit)
        if (remaining := response.headers.get("X-RateLimit-Remaining")) is not None:
            with suppress(ValueError):
                self.rate_remaining = int(remaining)

    async def lanes(self) -> list[Lane]:
        """Not rate limited: cache it, refresh on a schedule and after a failed quote."""
        response = await self.client.get("/v1/portal/ports")
        if response.status_code != 200:
            raise ApiError(response.status_code, _detail(response))
        return [
            Lane(
                origin=lane["origin"],
                destination=lane["destination"],
                airlines=tuple((a["code"], a["name"]) for a in lane["airlines"]),
            )
            for lane in response.json()["lanes"]
        ]

    async def brackets(self, airline: str) -> tuple[int, ...]:
        response = await self.client.get("/v1/portal/brackets", params={"airline": airline})
        if response.status_code == 404:
            raise NotOnPortal(_detail(response))
        if response.status_code != 200:
            raise ApiError(response.status_code, _detail(response))
        return tuple(response.json()["breaks_kg"])

    async def rate(
        self,
        origin: str,
        destination: str,
        airline: str,
        weight_kg: Decimal,
        volume_cbm: Decimal,
        *,
        not_after: datetime | None = None,
    ) -> RateOption:
        """One counted rate read. On 429 waits `Retry-After` when the caller's deadline allows, else raises."""
        params = {
            "origin": origin,
            "destination": destination,
            "airline": airline,
            "weight_kg": str(weight_kg),
            "volume_cbm": str(volume_cbm),
        }
        for attempt in range(2):
            if self.rate_remaining == 0 and self.window_reset_at and self.window_reset_at > _now():
                await self._wait((self.window_reset_at - _now()).total_seconds(), not_after, attempt)
            response = await self.client.get("/v1/portal/rates", params=params)
            self._read_limits(response)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "60"))
                self.window_reset_at = _now() + timedelta(seconds=retry_after)
                self.rate_remaining = 0
                await self._wait(retry_after, not_after, attempt)
                continue
            if response.status_code == 503:
                page = await self._page_rate(params) if self.page_url else None
                if isinstance(page, RateOption):
                    return page
                raise RatesUnavailable(_detail(response), page)
            if response.status_code == 404:
                raise NotOnPortal(_detail(response))
            if response.status_code != 200:
                raise ApiError(response.status_code, _detail(response))
            return _option(response.json())
        raise RateLimited(0)

    async def _page_rate(self, params: dict[str, str]) -> RateOption | str | None:
        """R21: the same rate from the portal's browser page, which stays up when the rate API is down (rounds 93
        to 97: the airlines and the desk did not answer by mail either). Every rate needs a fresh page load: it
        names the form's fields anew and draws a sum to solve. Without a rate, the last refusal (`_refusal`) or None."""
        values = {"origin": params["origin"], "destination": params["destination"], "airline": params["airline"],
                  "weight": params["weight_kg"], "volume": params["volume_cbm"]}  # fmt: skip
        refused = None
        for _ in range(PAGE_TRIES):
            load = await self.client.post(self.page_url, headers={"X-Portal-Step": "load"})
            if load.status_code != 200:
                return _refusal(load)
            try:
                form = load.json()
                answer = solve_sum(base64.b64decode(form["answer"]["image"].partition(",")[2]).decode())
                if answer is None:
                    continue
                data = {form["answer"]["name"]: str(answer), form["consent"]["name"]: form["consent"]["value"]}
                for field in form["fields"]:
                    data[field["name"]] = (
                        self.client.headers["X-API-Key"] if field["kind"] == "password"
                        else next(v for k, v in values.items() if k in field["label"].lower())
                    )  # fmt: skip
                response = await self.client.post(
                    self.page_url,
                    headers={"X-Portal-Challenge": form["id"]},
                    files={k: (None, v) for k, v in data.items()},
                )
                body = response.json()
                if response.status_code == 404:
                    raise NotOnPortal(str(body.get("error"))[:500])
                if body.get("html"):
                    return _option(page_rate(body["html"]))
                refused = _refusal(response)
            except (ValueError, ArithmeticError, LookupError, TypeError, StopIteration, AttributeError):
                return _refusal(load)  # not the page we know: the caller reports the API's 503
        return refused

    async def _wait(self, seconds: float, not_after: datetime | None, attempt: int) -> None:
        if attempt > 0 or (not_after is not None and _now() + timedelta(seconds=seconds) >= not_after):
            raise RateLimited(seconds)
        await asyncio.sleep(seconds)
        self.window_reset_at = None

    async def book(self, rate_id: str, team_reference: str) -> PortalBooking:
        """201 and 200 (retry) both return the booking. Raises RateExpired (410), BookingUnavailable (503)."""
        response = await self.client.post(
            "/v1/portal/bookings", json={"rate_id": rate_id, "team_reference": team_reference}
        )
        if response.status_code in (200, 201):
            body = response.json()
            return PortalBooking(
                booking_reference=body["booking_reference"],
                team_reference=body["team_reference"],
                rate_id=body["rate_id"],
                airline=body["airline"],
                price=Decimal(str(body["price"])),
                currency=body["currency"],
                status=body["status"],
            )
        if response.status_code == 410:
            raise RateExpired(_detail(response))
        if response.status_code == 503:
            raise BookingUnavailable(_detail(response))
        raise ApiError(response.status_code, _detail(response))

    async def cancel(self, booking_reference: str) -> None:
        """200, also when repeated; 404 for an unknown booking. Works for a booking made by mail too."""
        response = await self.client.delete(f"/v1/portal/bookings/{booking_reference}")
        if response.status_code != 200:
            raise ApiError(response.status_code, _detail(response))


def _option(rate: dict) -> RateOption:
    total = Decimal(str(rate["total"]))
    fx = Decimal(str(rate["fx_eur_per_usd"])) if rate.get("fx_eur_per_usd") else None
    chargeable = Decimal(str(rate["chargeable_weight_kg"]))
    return RateOption(
        source=Source.PORTAL,
        carrier=rate["airline"],
        carrier_code=rate.get("airline_code"),
        origin=rate["origin"],
        destination=rate["destination"],
        chargeable_kg=chargeable,
        billed_kg=chargeable,
        freight=total,
        total=total,
        currency=rate["currency"],
        fx_eur=fx,
        total_eur=to_eur(total, rate["currency"], fx),
        transit_days=int(rate["transit_days"]),
        rate_id=rate["rate_id"],
        valid_until=datetime.fromisoformat(rate["valid_until"]),
        dataset_version=str(rate["dataset_version"]) if rate.get("dataset_version") is not None else None,
    )
