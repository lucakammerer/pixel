from pixel.contracts import Booking
from pixel.core.context import Context


def save(ctx: Context, booking: Booking) -> None:
    raise NotImplementedError


def get(ctx: Context, booking_id: str) -> Booking | None:
    raise NotImplementedError


def by_reference(ctx: Context, booking_reference: str) -> Booking | None:
    raise NotImplementedError
