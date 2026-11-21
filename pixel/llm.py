"""The only module that calls the LLM gateway. Every call becomes an `llm.call` event with tokens and cost."""

import base64
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pixel.contracts import LlmResult
from pixel.core import events
from pixel.core.context import Context

# USD per 1M tokens (input, output) per model id, from OpenAI's price list (standard tier, read on 2026-11-21).
# The gateway reports tokens but no cost. A model missing here is logged with `cost_unknown`.
PRICES: dict[str, tuple[str, str]] = {
    "gpt-4o-mini": ("0.15", "0.60"),
    "gpt-4.1-mini": ("0.40", "1.60"),
    "gpt-5-nano": ("0.05", "0.40"),
    "gpt-5-mini": ("0.25", "2.00"),
    "gpt-5.4-nano": ("0.20", "1.25"),
    "gpt-5.4-mini": ("0.75", "4.50"),
    "gpt-5.6-luna": ("0.20", "1.20"),
    "gpt-6-luna": ("0.10", "0.50"),
}

SESSION_BUDGET_USD = Decimal(os.environ.get("PIXEL_SESSION_BUDGET_USD", "0.02"))


class BudgetExceeded(Exception):
    """The session already spent its budget; the caller continues without the model."""


def _price(model: str) -> tuple[Decimal, Decimal] | None:
    if model in PRICES:
        return tuple(Decimal(p) for p in PRICES[model])  # type: ignore[return-value]
    price_in, price_out = os.environ.get("PIXEL_LLM_PRICE_IN"), os.environ.get("PIXEL_LLM_PRICE_OUT")
    if price_in and price_out:
        return Decimal(price_in), Decimal(price_out)
    return None


def _cost(response: dict[str, Any], model: str, tokens_in: int, tokens_out: int) -> Decimal | None:
    usage = response.get("usage") or {}
    for value in (usage.get("cost"), usage.get("cost_usd"), response.get("cost"), response.get("cost_usd")):
        if value is not None:
            return Decimal(str(value))
    price = _price(model)
    if price is None:
        return None
    return (tokens_in * price[0] + tokens_out * price[1]) / Decimal(1_000_000)


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
    model: str | None = None,
) -> LlmResult:
    """One gateway call with a JSON schema answer, on `model` or the configured one. Raises on a call that would
    pass the session's budget."""
    if session_id is not None and spend_usd(ctx, session_id=session_id) >= SESSION_BUDGET_USD:
        raise BudgetExceeded(f"session {session_id} spent its budget")
    model = model or ctx.settings.model
    content: list[dict[str, Any]] | str = user
    if images:
        content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
            for i in images
        ]
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": re.sub(r"[^A-Za-z0-9_-]", "_", purpose)[:64], "schema": schema, "strict": True},
        },
        "max_completion_tokens": max_output_tokens,
    }
    started = time.monotonic()
    tokens_in = tokens_out = 0
    cost: Decimal | None = Decimal(0)
    try:
        response = await ctx.gateway.chat(payload)
        usage = response.get("usage") or {}
        tokens_in, tokens_out = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        cost = _cost(response, model, tokens_in, tokens_out)
        data = json.loads(response["choices"][0]["message"]["content"])
    except Exception as exc:
        events.emit(
            ctx,
            "llm.call",
            "llm",
            session_id=session_id,
            payload={"purpose": purpose, "model": model, "error": type(exc).__name__},
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost or Decimal(0),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    events.emit(
        ctx,
        "llm.call",
        "llm",
        session_id=session_id,
        payload={"purpose": purpose, "model": model, "cost_unknown": cost is None},
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost or Decimal(0),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return LlmResult(data=data, model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost or Decimal(0))


def spend_usd(ctx: Context, *, session_id: int | None = None, last_minutes: int | None = None) -> Decimal:
    """Gateway spend from the event log: for one session, or for the last minutes."""
    sql, args = "SELECT cost_usd FROM events WHERE kind = 'llm.call'", []
    if session_id is not None:
        sql += " AND session_id = ?"
        args.append(session_id)
    if last_minutes is not None:
        sql += " AND ts >= ?"
        args.append((datetime.now(UTC) - timedelta(minutes=last_minutes)).isoformat())
    return sum((Decimal(row[0]) for row in ctx.db.execute(sql, args)), Decimal(0))
