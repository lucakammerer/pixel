"""UTC wall time shared by deadlines and the offline replay clock."""

from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime

clock: ContextVar[Callable[[], datetime]] = ContextVar("clock", default=lambda: datetime.now(UTC))


def now() -> datetime:
    return clock.get()()
