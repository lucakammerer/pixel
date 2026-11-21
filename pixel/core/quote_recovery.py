"""A bounded second sourcing pass for an unanswered RFQ."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from pixel.agents.rfq import extract
from pixel.contracts import Message
from pixel.core import events, outbox, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, engine, portal_rates, session, store

SOURCE_BUDGET_S = 20


async def source(ctx: Context, message: Message, deadline: Deadline) -> None:
    """Publish options through the existing pricing sources while the orchestrator watches for a complete quote."""
    session_id = message.session_id
    started = timing.now()
    until = min(deadline.hard_at, started + timedelta(seconds=SOURCE_BUDGET_S))
    remaining = (until - started).total_seconds()
    if remaining <= 0 or outbox.quoted(ctx, session_id) or outbox.taken_over(ctx, session_id):
        return
    try:
        async with asyncio.timeout(remaining):
            rfq = session.rfq(ctx, session_id)
            if rfq is None:
                # A completed or failed model read already had the rules-based guess as its fallback.
                if ctx.db.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND kind='llm.call' "
                    "AND json_extract(payload,'$.purpose')='rfq.extract' LIMIT 1",
                    (session_id,),
                ).fetchone():
                    return
                # Rules that cannot read it sent the handler to the model, the request's one call, even when the
                # takeover cancelled it before its `llm.call` was written.
                if extract.by_rules(message, deadline) is None:
                    return
                rfq = await extract.extract(ctx, message, deadline)
            if not rfq.origin or not rfq.destination or rfq.weight_kg <= 0:
                return
            if outbox.quoted(ctx, session_id) or outbox.taken_over(ctx, session_id):
                return
            options = session.for_request(ctx, rfq)
            if not any(not option.trucker for option in options):
                engine.sheet_options(ctx, rfq)
                options = session.for_request(ctx, rfq)
            async with asyncio.TaskGroup() as tasks:
                if not any(not option.trucker for option in options):
                    tasks.create_task(portal_rates.options(ctx, rfq, replace(deadline, target_at=until)))
                if rfq.door_pickup and rfq.pickup_zone and not any(option.trucker for option in options):
                    asked = ctx.db.execute(
                        "SELECT recipient FROM messages WHERE session_id=? AND purpose='bid_pickup' AND subject=?",
                        (session_id, bids._subject(rfq, pickup=True)),
                    ).fetchall()
                    recipients = {row["recipient"] for row in asked}
                    truckers = [
                        (name, email)
                        for name, email in store.carriers(ctx, "trucker", rfq.pickup_zone)
                        if email.lower() not in recipients
                    ]
                    if truckers:
                        tasks.create_task(bids.request(ctx, rfq, truckers, pickup=True))
    except Exception as exc:  # noqa: BLE001  the deadline path continues independently
        events.emit(ctx, "error", "quote_recovery", session_id=session_id,
                    payload={"where": "source", "error": type(exc).__name__})  # fmt: skip
