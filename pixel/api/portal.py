"""GET /v1/portal/ports, /brackets, /rates and POST /v1/portal/bookings."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from pixel.api.client import ApiError
from pixel.contracts import RateOption, Source
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
    return datetime.now(UTC)


class Portal:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.rate_limit: int | None = None  # X-RateLimit-Limit
        self.rate_remaining: int | None = None  # X-RateLimit-Remaining
        self.window_reset_at: datetime | None = None  # from the last 429's Retry-After

    def _read_limits(self, response: httpx.Response) -> None:
        if (limit := response.headers.get("X-RateLimit-Limit")) is not None:
            self.rate_limit = int(limit)
        if (remaining := response.headers.get("X-RateLimit-Remaining")) is not None:
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
            if response.status_code in (404, 503):
                raise NotOnPortal(_detail(response))
            if response.status_code != 200:
                raise ApiError(response.status_code, _detail(response))
            return _option(response.json())
        raise RateLimited(0)

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
