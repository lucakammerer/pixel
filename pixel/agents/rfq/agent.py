import asyncio
import json
from datetime import UTC, datetime, timedelta

from pixel import llm
from pixel.agents.rfq import compose, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import events, feedback, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import engine, selection, session

REQUIRED = {"origin", "destination", "weight_kg"}
PROSE_WAIT_S = 10


def _quote(ctx: Context, rfq: Rfq, options: list[RateOption], prose: compose.Prose | None, fallback: bool) -> Quote:
    option = selection.select(rfq, options)
    subject, body = compose.render(
        rfq,
        option,
        prose,
        compose.customer_subject(ctx, rfq.message_id),
        ctx.settings.signer,
        compose.customer_body(ctx, rfq.message_id),
    )
    calls = ctx.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'llm.call' AND session_id = ?", (rfq.session_id,)
    ).fetchone()[0]
    quote = Quote(
        id=quotes.new_id(ctx),
        session_id=rfq.session_id,
        rfq=rfq,
        option=option,
        total=option.total,
        currency=option.currency,
        subject=subject,
        body=body,
        fallback=fallback,
        llm_calls=calls,
        llm_cost_usd=llm.spend_usd(ctx, session_id=rfq.session_id),
        created_at=datetime.now(UTC),
    )
    events.emit(ctx, "quote.drafted", "rfq", session_id=rfq.session_id,
                payload={"quote_id": quote.id, "option": option.model_dump(mode="json"), "fallback": fallback,
                         "options": len(options)})  # fmt: skip
    quotes.save(ctx, quote)  # booking finds the session's quote in the CRM, also when the fallback sends it
    return quote


async def handle(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """extract -> pricing.engine.options (emits `option.found` as they arrive) -> selection.select ->
    compose.render -> outbox.send_quote. Sends at `deadline.target_at` at the latest unless waiting can
    still change the answer and `hard_at` is not reached."""
    rfq = await extract.extract(ctx, message, deadline)
    if REQUIRED & set(rfq.missing):
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"error": "unreadable request",
                    "missing": list(rfq.missing)})  # fmt: skip
        return None
    # The one gateway call per quote (R28): when rules read the request, it writes the prose slots (R31);
    # when the model read it, the same call already drafted them.
    # The prose may hold the quote at most PROSE_WAIT_S after it started, and never past target_at: a slow or
    # broken model call costs us the human slots, never the quote or its time points.
    task = None
    if rfq.extracted_by == "rules":
        task = asyncio.create_task(compose.prose(ctx, rfq, message))
        prose_by = min(datetime.now(UTC) + timedelta(seconds=PROSE_WAIT_S), deadline.target_at)
    else:
        compose.adopt(ctx, rfq, message, _raw_prose(ctx, rfq.session_id))
    options = await engine.options(ctx, rfq, deadline)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), max((prose_by - datetime.now(UTC)).total_seconds(), 0.0))
        except Exception:  # noqa: BLE001, S110  late or failed: the quote goes out with the fixed phrases
            pass  # a late task keeps running and stores its slots for the fallback
    prose = compose.stored(ctx, rfq.session_id)
    if not options:
        return None
    sent = await outbox.send_quote(ctx, _quote(ctx, rfq, options, prose, fallback=False))
    quotes.save(ctx, sent)
    return sent


async def fallback(ctx: Context, session_id: int) -> Quote | None:
    """The best quote from the options stored so far, built by code alone. None when there is no price."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    options = engine.combine(rfq, session.options(ctx, session_id), feedback.pickup_rule(ctx))
    return _quote(ctx, rfq, options, compose.stored(ctx, session_id), fallback=True) if options else None


def _raw_prose(ctx: Context, session_id: int) -> dict:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'prose.raw' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    try:
        return json.loads(row["payload"]) if row else {}
    except ValueError:
        return {}
