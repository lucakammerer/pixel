"""Routes each mail to its handler and holds every request to its own deadline."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pixel.config import Settings
from pixel.contracts import Message
from pixel.core.context import Context

EXPIRES_AFTER = timedelta(minutes=6)


@dataclass(frozen=True)
class Deadline:
    received_at: datetime
    target_at: datetime  # send by here for full time points
    hard_at: datetime  # the fallback sends whatever we have
    expires_at: datetime


def deadline_for(received_at: datetime, settings: Settings) -> Deadline:
    return Deadline(
        received_at=received_at,
        target_at=received_at + timedelta(seconds=settings.quote_target_s),
        hard_at=received_at + timedelta(seconds=settings.quote_hard_s),
        expires_at=received_at + EXPIRES_AFTER,
    )


async def dispatch(ctx: Context, message: Message) -> None:
    """router.classify, then: customer_rfq -> quote_with_deadline; customer_reply -> rfq.booking.handle_reply;
    carrier_bid, ratedesk_reply -> pricing.bids.record; booking_confirmation -> rfq.booking.handle_confirmation;
    tariff -> tariffs.handle; tracking_question -> tracking.handle. An exception becomes an `error` event."""
    raise NotImplementedError


async def quote_with_deadline(ctx: Context, message: Message) -> None:
    """Runs rfq.agent.handle as a task. If no quote is sent by `hard_at`, sends rfq.agent.fallback's quote and
    cancels the task; if that has no price either, emits `quote.missed`."""
    raise NotImplementedError


async def recover(ctx: Context) -> None:
    """After a restart: every session still inside its deadline without a sent quote gets its task back."""
    raise NotImplementedError
