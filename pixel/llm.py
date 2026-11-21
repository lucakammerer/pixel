"""The only module that calls the LLM gateway. Every call becomes an `llm.call` event with tokens and cost."""

from decimal import Decimal
from typing import Any

from pixel.contracts import LlmResult
from pixel.core.context import Context

# USD per 1M tokens (input, output) per model id; filled from the gateway's usage data, never guessed.
PRICES: dict[str, tuple[str, str]] = {}


async def ask_json(
    ctx: Context,
    *,
    purpose: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    session_id: int | None = None,
    images: tuple[bytes, ...] = (),
    max_output_tokens: int = 300,
) -> LlmResult:
    """One gateway call with a JSON schema answer. Raises on a call that would pass the session's budget."""
    raise NotImplementedError


def spend_usd(ctx: Context, *, session_id: int | None = None, last_minutes: int | None = None) -> Decimal:
    """Gateway spend from the event log: for one session, or for the last minutes."""
    raise NotImplementedError
