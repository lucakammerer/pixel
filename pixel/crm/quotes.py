from typing import Any

from pixel.contracts import Quote, QuoteStatus
from pixel.core.context import Context


def new_id(ctx: Context) -> str:
    raise NotImplementedError


def save(ctx: Context, quote: Quote) -> None:
    raise NotImplementedError


def get(ctx: Context, quote_id: str) -> Quote | None:
    raise NotImplementedError


def for_session(ctx: Context, session_id: int) -> Quote | None:
    raise NotImplementedError


def set_status(ctx: Context, quote_id: str, status: QuoteStatus) -> None:
    raise NotImplementedError


def stats(ctx: Context) -> dict[str, Any]:
    """Dashboard numbers: requests, quoted, missed, accepted and rejected by the scorer, won and lost with the
    customer, points per kind, spend per accepted quote this hour, median response time."""
    raise NotImplementedError
