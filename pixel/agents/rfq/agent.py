import asyncio
import json
from datetime import timedelta
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import check, compose, dangerous, extract
from pixel.contracts import Message, Preference, Quote, RateOption, Rfq
from pixel.core import events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import calc, engine, portal_rates, selection, session, store

REQUIRED = {"origin", "destination", "weight_kg"}
PROSE_WAIT_S = 10  # the quote's text may hold the quote this long, the prose slots or the draft
DRAFT_MIN_S = 1.0  # with less time left the template goes at once
# Bids are waited for until target_at, so the option is often chosen just then. The draft may hold the quote this
# much past it, never past hard_at: target_at (100 s) lies inside the scorer's full-time window (R29, 2 minutes),
# and a quote without its gateway call earns no cost points (R28).
DRAFT_PAST_TARGET_S = 10.0
AIRPORTS_WAIT_S = 5.0
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


def _fx_for(currency: str, options: list[RateOption]) -> Decimal | None:
    """The exchange rate (EUR per unit) one of this request's own options carries. Never one from another session:
    an older dataset's rate would put the quote off the scorer's price."""
    if currency == "EUR":
        return Decimal(1)
    return next((o.fx_eur for o in options if o.currency == currency and o.fx_eur and o.fx_eur > 0), None)


def _chosen(rfq: Rfq, options: list[RateOption]) -> RateOption:
    option = selection.select(rfq, options)
    if rfq.currency and option.currency != rfq.currency:
        option = calc.in_currency(option, rfq.currency, _fx_for(rfq.currency, options))
    return option


def _quote(
    ctx: Context,
    rfq: Rfq,
    options: list[RateOption],
    prose: compose.Prose | None,
    fallback: bool,
    drafted: tuple[RateOption, str] | None = None,
) -> Quote:
    """The quote for the chosen option: the model's drafted body when it was written for this option, else the
    template (`compose.render`)."""
    option = _chosen(rfq, options)
    subject, body = compose.render(
        rfq,
        option,
        prose,
        compose.customer_subject(ctx, rfq.message_id),
        ctx.settings.signer,
        compose.customer_body(ctx, rfq.message_id),
    )
    if drafted and drafted[0] == option:
        body = drafted[1]
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
        created_at=timing.now(),
    )
    quote = _noted(ctx, rfq, quote)
    _check(ctx, rfq, quote)
    events.emit(ctx, "quote.drafted", "rfq", session_id=rfq.session_id,
                payload={"quote_id": quote.id, "option": option.model_dump(mode="json"), "fallback": fallback,
                         "options": len(options), "complete_service": not rfq.door_pickup or bool(option.trucker)})  # fmt: skip
    quotes.save(ctx, quote)  # booking finds the session's quote in the CRM, also when the fallback sends it
    return quote


def _check(ctx: Context, rfq: Rfq, quote: Quote) -> None:
    """The last look before the quote can leave (`check.py`): a `quote.checked` event with what it found. It never
    holds or changes the quote; a BLOCK finding is an issue for `feedback.alert`, so a person sees it at once.
    PIXEL_PRECHECK=off skips it. Never raises."""
    if ctx.settings.precheck == "off":
        return
    try:
        row = ctx.db.execute("SELECT data FROM messages WHERE id = ?", (rfq.message_id,)).fetchone()
        message = Message.model_validate_json(row["data"]) if row else None
        request = extract._text(message) if message else ""
        files = tuple(a.filename for a in message.attachments) if message else ()
        findings = check.run(quote, request, session.for_request(ctx, rfq), files)
        events.emit(ctx, "quote.checked", "rfq", session_id=rfq.session_id,
                    payload={"quote": quote.id, "blocking": check.blocking(findings), "fallback": quote.fallback,
                             "total": str(quote.total), "findings": check.as_payload(findings)})  # fmt: skip
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "check", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip


def _noted(ctx: Context, rfq: Rfq, quote: Quote) -> Quote:
    """The quote with the dangerous-goods note, when the request's documents declare some (`dangerous.py`), unless
    its drafted body already names the goods and asks for the DGD and the MSDS."""
    found = dangerous.declared(ctx, rfq)
    if not found:
        return quote
    goods, source = found
    if goods in quote.body and "DGD" in quote.body and "MSDS" in quote.body:
        return quote
    return quote.model_copy(update={"body": dangerous.with_note(quote.body, goods, compose.language_of(rfq), source)})


async def handle(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """extract -> pricing.engine.options (emits `option.found` as they arrive) -> selection.select ->
    compose.render -> outbox.send_quote. Sends at `deadline.target_at` at the latest unless waiting can
    still change the answer and `hard_at` is not reached."""
    rfq, text = await extract.read_request(ctx, message, deadline)
    if REQUIRED & set(rfq.missing):
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"error": "unreadable request",
                    "missing": list(rfq.missing)})  # fmt: skip
        return None
    rfq = await served_airports(ctx, rfq, message, text=text, deadline=deadline)
    # The one gateway call per quote (R28): when rules read the request, it drafts the whole body once the option
    # is chosen (PIXEL_DRAFT, `_drafted`), or with PIXEL_DRAFT=off writes the prose slots alongside the pricing
    # (R31); when the model read it, the same call already drafted the slots.
    # The text may hold the quote at most PROSE_WAIT_S after it started, the slots never past target_at, the draft
    # never more than DRAFT_PAST_TARGET_S past it: a slow or broken model call costs us the human text, never the
    # quote or its time points.
    task = None
    draft = ctx.settings.draft_quotes and rfq.extracted_by == "rules"
    if rfq.extracted_by == "rules" and not draft:
        task = poller.spawn(ctx, compose.prose(ctx, rfq, message))
        prose_by = min(timing.now() + timedelta(seconds=PROSE_WAIT_S), deadline.target_at)
    elif rfq.extracted_by != "rules":
        compose.adopt(ctx, rfq, message, _raw_prose(ctx, rfq.session_id))
    options = await engine.options(ctx, rfq, deadline)
    if rfq.door_pickup:
        options = [o for o in options if o.trucker]
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), max((prose_by - timing.now()).total_seconds(), 0.0))
        except Exception:  # noqa: BLE001, S110  late or failed: the quote goes out with the fixed phrases
            pass  # a late task keeps running and stores its slots for the fallback
    if not options:
        if draft:  # the orchestrator's fallback quotes later without a model call: the slots are this quote's call
            poller.spawn(ctx, compose.prose(ctx, rfq, message))
        return None
    drafted = await _drafted(ctx, rfq, options, message, deadline) if draft else None
    prose = compose.stored(ctx, rfq.session_id)
    sent = await outbox.send_quote(ctx, _quote(ctx, rfq, options, prose, fallback=False, drafted=drafted))
    quotes.save(ctx, sent)
    return sent


def _claim(rfq: Rfq, option: RateOption, options: list[RateOption]) -> str | None:
    """What the draft may call the option: "cheapest" or "fastest", the customer's wish, only when code finds it
    is that among two or more options."""
    if len(options) < 2:
        return None
    if rfq.preference == Preference.FASTEST:
        return "fastest" if option.transit_days <= min(o.transit_days for o in options) else None
    return "cheapest" if option.total_eur <= min(o.total_eur for o in options) else None


async def _drafted(
    ctx: Context, rfq: Rfq, options: list[RateOption], message: Message, deadline: Deadline
) -> tuple[RateOption, str] | None:
    """The model's whole body for the chosen option (`compose.draft`), waited for at most PROSE_WAIT_S, never more
    than DRAFT_PAST_TARGET_S past target_at and never past hard_at; with DRAFT_MIN_S or less left, or on any
    failure, None and the template goes out."""
    option = _chosen(rfq, options)
    latest = min(deadline.target_at + timedelta(seconds=DRAFT_PAST_TARGET_S), deadline.hard_at)
    budget = min(float(PROSE_WAIT_S), (latest - timing.now()).total_seconds())
    if budget <= DRAFT_MIN_S:
        events.emit(ctx, "quote.prose", "rfq", session_id=rfq.session_id,
                    payload={"mode": "draft", "passed": False, "reason": "no time left"})  # fmt: skip
        return None
    found = dangerous.declared(ctx, rfq)
    try:
        body = await asyncio.wait_for(
            compose.draft(ctx, rfq, option, message, dg=found[0] if found else None,
                          claimed_wish=_claim(rfq, option, options)),
            timeout=budget,
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001  late or failed: the template goes out
        events.emit(ctx, "quote.prose", "rfq", session_id=rfq.session_id,
                    payload={"mode": "draft", "passed": False, "reason": f"timeout: {type(exc).__name__}"})  # fmt: skip
        return None
    return (option, body) if body else None


async def served_airports(
    ctx: Context, rfq: Rfq, message: Message, *, text: str | None = None, deadline: Deadline | None = None
) -> Rfq:
    """A guessed airport that no lane we know starts or ends at (a nearby airfield, a city code) prices nothing,
    and its bid requests are wasted. Such a code, when the mail does not name it, goes back to the model once with
    the airports we serve; a code the mail names stands. No call when both codes are served."""
    text = extract._text(message) if text is None else text
    origins, destinations = store.airports(ctx)
    wrong = {
        key: served
        for key, served in (("origin", origins), ("destination", destinations))
        if getattr(rfq, key) not in served and not extract.names_code(text, getattr(rfq, key))
    }
    if not wrong:
        return rfq
    # The optional check shares one short budget across both APIs. Leave at least 90% of the remaining
    # sourcing window to actual prices, including when a restart picks this RFQ up close to its deadline.
    available = AIRPORTS_WAIT_S
    if deadline:
        remaining = (min(deadline.target_at, deadline.hard_at) - timing.now()).total_seconds()
        available = min(available, remaining / 10)
    if available <= 0:
        return rfq
    loop = asyncio.get_running_loop()
    until = loop.time() + available
    try:
        for lane in await asyncio.wait_for(portal_rates.lanes(ctx), timeout=available / 4):
            origins.add(lane.origin)
            destinations.add(lane.destination)
    except Exception as exc:  # noqa: BLE001  the local tariffs and carrier list still apply
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "served_airports", "error": type(exc).__name__})  # fmt: skip
    wrong = {key: served for key, served in wrong.items() if served and getattr(rfq, key) not in served}
    if not wrong:
        return rfq
    user = (f"{text}\n\nRead: {rfq.origin} to {rfq.destination}.\nDeparture airports we serve: "
            f"{' '.join(sorted(origins))}\nDestination airports we serve: {' '.join(sorted(destinations))}")  # fmt: skip
    try:
        result = await asyncio.wait_for(
            llm.ask_json(
                ctx,
                purpose="rfq.airports",
                system=AIRPORTS_SYSTEM,
                user=user,
                schema=AIRPORTS_SCHEMA,
                session_id=rfq.session_id,
                max_output_tokens=40,
            ),
            timeout=max(0.0, until - loop.time()),
        )
    except Exception as e:  # noqa: BLE001  pricing goes on with the codes as read
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "served_airports", "error": type(e).__name__})  # fmt: skip
        return rfq
    update = {k: code for k, served in wrong.items() if (code := str(result.data.get(k) or "").upper()) in served}
    if not update:
        return rfq
    rfq = rfq.model_copy(update=update)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                payload=rfq.model_dump(mode="json"))  # fmt: skip
    return rfq


async def fallback(ctx: Context, session_id: int, *, allow_partial: bool = False) -> Quote | None:
    """The best complete price so far. At the hard deadline an air-only price can cover a missing pickup."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    options = engine.combine(rfq, session.for_request(ctx, rfq), feedback.pickup_rule(ctx))
    if rfq.door_pickup and not allow_partial:
        options = [o for o in options if o.trucker]
    if not options:
        return None
    return _quote(ctx, rfq, options, compose.stored(ctx, session_id), fallback=True)


def _raw_prose(ctx: Context, session_id: int) -> dict:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'prose.raw' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    try:
        return json.loads(row["payload"]) if row else {}
    except ValueError:
        return {}
