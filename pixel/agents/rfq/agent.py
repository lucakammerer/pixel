import asyncio
from datetime import UTC, datetime

from pixel import llm
from pixel.agents.rfq import compose, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import engine, selection, session

REQUIRED = {"origin", "destination", "weight_kg"}
OPENING_WAIT_S = 10


def _quote(ctx: Context, rfq: Rfq, options: list[RateOption], opening: str | None, fallback: bool) -> Quote:
    option = selection.select(rfq, options)
    subject, body = compose.render(rfq, option, opening)
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
    # The one gateway call per quote (R28): when rules read the request, it writes the opening line instead.
    opening = asyncio.create_task(compose.opening(ctx, rfq, message)) if rfq.extracted_by == "rules" else None
    options = await engine.options(ctx, rfq, deadline)
    line = None
    if opening:
        try:
            line = await asyncio.wait_for(opening, OPENING_WAIT_S)
        except TimeoutError:
            line = None
    if not options:
        return None
    return await outbox.send_quote(ctx, _quote(ctx, rfq, options, line, fallback=False))


async def fallback(ctx: Context, session_id: int) -> Quote | None:
    """The best quote from the options stored so far, built by code alone. None when there is no price."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    options = engine.combine(rfq, session.options(ctx, session_id))
    return _quote(ctx, rfq, options, None, fallback=True) if options else None
