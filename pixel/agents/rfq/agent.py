import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from pixel import llm
from pixel.agents.rfq import compose, dangerous, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import events, feedback, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import engine, portal_rates, selection, session, store

REQUIRED = {"origin", "destination", "weight_kg"}
PROSE_WAIT_S = 10
AIRPORTS_SYSTEM = (
    "You check the airports of an air freight request. A code read from the mail is on none of the lanes we "
    "price. Answer the IATA codes of the departure and the destination airport. Keep a code the mail names "
    "itself; otherwise pick from the airports we serve the one the mail's place belongs to, or null."
)
AIRPORTS_SCHEMA = {
    "type": "object",
    "properties": {"origin": {"type": ["string", "null"]}, "destination": {"type": ["string", "null"]}},
    "required": ["origin", "destination"],
    "additionalProperties": False,
}


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


def _noted(ctx: Context, rfq: Rfq, quote: Quote) -> Quote:
    """The quote with the dangerous-goods note, when the request's documents declare some (`dangerous.py`)."""
    found = dangerous.declared(ctx, rfq)
    if not found:
        return quote
    goods, source = found
    return quote.model_copy(update={"body": dangerous.with_note(quote.body, goods, compose.language_of(rfq), source)})


async def handle(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """extract -> pricing.engine.options (emits `option.found` as they arrive) -> selection.select ->
    compose.render -> outbox.send_quote. Sends at `deadline.target_at` at the latest unless waiting can
    still change the answer and `hard_at` is not reached."""
    rfq = await extract.extract(ctx, message, deadline)
    if REQUIRED & set(rfq.missing):
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"error": "unreadable request",
                    "missing": list(rfq.missing)})  # fmt: skip
        return None
    rfq = await served_airports(ctx, rfq, message)
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
    sent = await outbox.send_quote(ctx, _noted(ctx, rfq, _quote(ctx, rfq, options, prose, fallback=False)))
    quotes.save(ctx, sent)
    return sent


async def _served(ctx: Context) -> tuple[set[str], set[str]]:
    origins, destinations = store.airports(ctx)
    try:
        for lane in await portal_rates.lanes(ctx):
            origins.add(lane.origin)
            destinations.add(lane.destination)
    except Exception as e:  # noqa: BLE001  the sheets and the carrier list still tell
        events.emit(ctx, "error", "rfq", payload={"where": "served_airports", "error": f"{type(e).__name__}: {e}"})
    return origins, destinations


async def served_airports(ctx: Context, rfq: Rfq, message: Message) -> Rfq:
    """A guessed airport that no lane we know starts or ends at (a nearby airfield, a city code) prices nothing,
    and its bid requests are wasted. Such a code, when the mail does not name it, goes back to the model once with
    the airports we serve; a code the mail names stands. No call when both codes are served."""
    origins, destinations = await _served(ctx)
    text = f"{message.subject}\n\n{message.body}"
    wrong = {
        key: served
        for key, served in (("origin", origins), ("destination", destinations))
        if served and getattr(rfq, key) not in served and not re.search(rf"\b{getattr(rfq, key)}\b", text)
    }
    if not wrong:
        return rfq
    user = (f"{text}\n\nRead: {rfq.origin} to {rfq.destination}.\nDeparture airports we serve: "
            f"{' '.join(sorted(origins))}\nDestination airports we serve: {' '.join(sorted(destinations))}")  # fmt: skip
    try:
        result = await llm.ask_json(ctx, purpose="rfq.airports", system=AIRPORTS_SYSTEM, user=user,
                                    schema=AIRPORTS_SCHEMA, session_id=rfq.session_id, max_output_tokens=40)  # fmt: skip
    except Exception as e:  # noqa: BLE001  pricing goes on with the codes as read
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "served_airports", "error": f"{type(e).__name__}: {e}"})  # fmt: skip
        return rfq
    update = {k: code for k, served in wrong.items() if (code := str(result.data.get(k) or "").upper()) in served}
    if not update:
        return rfq
    rfq = rfq.model_copy(update=update)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                payload=rfq.model_dump(mode="json"))  # fmt: skip
    return rfq


async def fallback(ctx: Context, session_id: int) -> Quote | None:
    """The best quote from the options stored so far, built by code alone. None when there is no price."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    options = engine.combine(rfq, session.options(ctx, session_id), feedback.pickup_rule(ctx))
    if not options:
        return None
    return _noted(ctx, rfq, _quote(ctx, rfq, options, compose.stored(ctx, session_id), fallback=True))


def _raw_prose(ctx: Context, session_id: int) -> dict:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'prose.raw' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    try:
        return json.loads(row["payload"]) if row else {}
    except ValueError:
        return {}
