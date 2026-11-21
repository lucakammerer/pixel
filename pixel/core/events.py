"""The event log: the one record of every mail, decision and model call. CRM and UI read it, nothing else."""

from collections.abc import AsyncIterator
from typing import Any

from pixel.contracts import Event
from pixel.core.context import Context

KINDS = (
    "mail.in", "mail.out", "mail.held",  # held: dry run, or a session a person took over
    "route",
    "rfq.extracted", "option.found", "option.failed", "bid.requested", "bid.received",
    "quote.drafted", "quote.sent", "quote.fallback", "quote.missed",
    "customer.accepted", "customer.declined",
    "booking.requested", "booking.booked", "booking.confirmed", "booking.failed",
    "tracking.answered",
    "tariff.ingested", "tariff.failed",
    "llm.call",
    "score",  # one row of /v1/board/me
    "human.note", "human.takeover", "human.release",
    "error",
)  # fmt: skip


def emit(
    ctx: Context,
    kind: str,
    actor: str,
    *,
    session_id: int | None = None,
    message_id: int | None = None,
    payload: dict[str, Any] | None = None,
    **metrics,
) -> Event:
    """Stores the event and wakes every subscriber. Payloads never hold a key."""
    raise NotImplementedError


def for_session(ctx: Context, session_id: int, after_id: int = 0) -> list[Event]:
    raise NotImplementedError


def subscribe(session_id: int | None = None) -> AsyncIterator[Event]:
    """Live events, for one session or all. Feeds the SSE streams in `pixel/ui/`."""
    raise NotImplementedError
