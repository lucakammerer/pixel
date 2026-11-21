"""GET /v1/portal/ports, /brackets, /rates and POST /v1/portal/bookings."""

from dataclasses import dataclass
from decimal import Decimal

import httpx

from pixel.contracts import RateOption


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


class Portal:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.rate_limit: int | None = None  # X-RateLimit-Limit
        self.rate_remaining: int | None = None  # X-RateLimit-Remaining

    async def lanes(self) -> list[Lane]:
        """Not rate limited: cache it, refresh on a schedule and after a failed quote."""
        raise NotImplementedError

    async def brackets(self, airline: str) -> tuple[int, ...]:
        raise NotImplementedError

    async def rate(
        self, origin: str, destination: str, airline: str, weight_kg: Decimal, volume_cbm: Decimal
    ) -> RateOption:
        """One counted rate read. On 429 waits `Retry-After` when the caller's deadline allows, else raises."""
        raise NotImplementedError

    async def book(self, rate_id: str, team_reference: str) -> PortalBooking:
        """201 and 200 (retry) both return the booking. Raises RateExpired (410), BookingUnavailable (503)."""
        raise NotImplementedError
