"""The event log: the one record of every mail, decision and model call. CRM and UI read it, nothing else."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pixel.contracts import Event
from pixel.core.context import Context

KINDS = (
    "mail.in", "mail.out", "mail.held",  # held: dry run, or a session a person took over
    "route",
    "rfq.extracted", "rfq.dangerous_goods", "option.found", "option.failed", "bid.requested", "bid.received",
    "quote.drafted", "quote.sent", "quote.fallback", "quote.missed", "quote.prose", "prose.raw",
    "customer.accepted", "customer.declined",
    "booking.requested", "booking.booked", "booking.confirmed", "booking.failed",
    "tracking.answered",
    "tariff.ingested", "tariff.failed",
    "llm.call",
    "score",  # one row of /v1/board/me
    "feedback",  # what a request scored and which option the scorer's best price was (core/feedback.py)
    "human.note", "human.takeover", "human.release",
    "error",
)  # fmt: skip

_subscribers: set[tuple[int | None, asyncio.Queue[Event]]] = set()


def scrub(ctx: Context, text: str) -> str:
    """Removes our key from any text that leaves the process or lands in the log."""
    key = ctx.settings.api_key
    return text.replace(key, "***") if key else text


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
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind}")
    payload_json = scrub(ctx, json.dumps(payload or {}, default=str))
    ts = datetime.now(UTC)
    event = Event(
        ts=ts,
        session_id=session_id,
        kind=kind,
        actor=actor,
        message_id=message_id,
        payload=json.loads(payload_json),
        tokens_in=metrics.get("tokens_in", 0),
        tokens_out=metrics.get("tokens_out", 0),
        cost_usd=Decimal(str(metrics.get("cost_usd", 0))),
        duration_ms=metrics.get("duration_ms", 0),
    )
    cursor = ctx.db.execute(
        "INSERT INTO events (ts, session_id, kind, actor, message_id, payload, tokens_in, tokens_out, cost_usd,"
        " duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), session_id, kind, actor, message_id, payload_json, event.tokens_in, event.tokens_out,
         str(event.cost_usd), event.duration_ms),
    )  # fmt: skip
    event = event.model_copy(update={"id": cursor.lastrowid})
    for wanted, queue in _subscribers:
        if wanted is None or wanted == session_id:
            queue.put_nowait(event)
    return event


def _row(row) -> Event:
    return Event(
        id=row["id"],
        ts=datetime.fromisoformat(row["ts"]),
        session_id=row["session_id"],
        kind=row["kind"],
        actor=row["actor"],
        message_id=row["message_id"],
        payload=json.loads(row["payload"]),
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=Decimal(row["cost_usd"]),
        duration_ms=row["duration_ms"],
    )


def for_session(ctx: Context, session_id: int, after_id: int = 0) -> list[Event]:
    rows = ctx.db.execute(
        "SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id", (session_id, after_id)
    ).fetchall()
    return [_row(r) for r in rows]


def since(ctx: Context, after_id: int = 0, limit: int = 500) -> list[Event]:
    rows = ctx.db.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
    return [_row(r) for r in rows]


async def subscribe(session_id: int | None = None) -> AsyncIterator[Event]:
    """Live events, for one session or all. Feeds the SSE streams in `pixel/ui/`."""
    entry: tuple[int | None, asyncio.Queue[Event]] = (session_id, asyncio.Queue())
    _subscribers.add(entry)
    try:
        while True:
            yield await entry[1].get()
    finally:
        _subscribers.discard(entry)
