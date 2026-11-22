import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import check, compose, dangerous, extract, novelty
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import db, events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline, deadline_for
from pixel.crm import quotes
from pixel.pricing import calc, engine, portal_rates, selection, session, store

REQUIRED = {"origin", "destination", "weight_kg"}
PROSE_WAIT_S = 10
TRANSLATE_WAIT_S = 10
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


async def _with_shipments(quote: Quote, others: list[Rfq], pricing: list[asyncio.Task], deadline: Deadline) -> Quote:
    """The mail's further shipments, each priced and selected like the first, in the same quote (R4). One that has
    no price by `target_at` is left out, and with it the sum of all."""
    wait = max((deadline.target_at - timing.now()).total_seconds(), 0.0)
    done, _ = await asyncio.wait(pricing, timeout=wait)
    priced = []
    for rfq, task in zip(others, pricing, strict=True):
        if task not in done or task.exception():
            continue
        options = [o for o in task.result() if o.trucker or not rfq.door_pickup]
        if not (options := selection.compliant_options(rfq, options)):
            continue
        option = calc.in_currency(selection.select(rfq, options), quote.currency, _fx_for(quote.currency, options))
        if option.currency == quote.currency:  # never a sum across currencies
            priced.append((rfq, option))
    total = quote.total + sum((o.total for _, o in priced), Decimal(0)) if len(priced) == len(others) else None
    return quote.model_copy(update={"body": compose.shipments(quote.body, quote.rfq, priced, total)})


def _quote(ctx: Context, rfq: Rfq, options: list[RateOption], prose: compose.Prose | None, fallback: bool) -> Quote:
    required = novelty.mapped(ctx, rfq.session_id)
    request = compose.customer_body(ctx, rfq.message_id)
    arrive_by = date.fromisoformat(required["arrive_by"]) if not rfq.arrive_by and required.get("arrive_by") else None
    ready = (compose.ready_date(request) or calc.ready_day(rfq)) if arrive_by else None
    option = selection.select(rfq, options, only=required.get("only_airlines") or (), arrive_by=arrive_by, ready=ready)
    if not selection.requested_carrier(rfq, option.carrier, option.carrier_code):
        required = {**required, "unmet_carrier": True}
    if not selection.meets_schedule(rfq, option):
        required = {**required, "unmet_schedule": True}
    if rfq.currency and option.currency != rfq.currency:
        option = calc.in_currency(option, rfq.currency, _fx_for(rfq.currency, options))
    premium = _insurance(ctx, rfq, request, options, option)
    if premium:
        option = calc.with_insurance(option, premium)
    subject, body = compose.render(
        rfq, option, prose, compose.customer_subject(ctx, rfq.message_id), ctx.settings.signer, request
    )
    if premium:
        body = compose.insured(body, rfq)
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
    if required:
        quote = quote.model_copy(update={"body": novelty.with_note(quote.body, required, compose.language_of(rfq))})
    _check(ctx, rfq, quote, premium or Decimal(0))
    events.emit(ctx, "quote.drafted", "rfq", session_id=rfq.session_id,
                payload={"quote_id": quote.id, "option": option.model_dump(mode="json"), "fallback": fallback,
                         "options": len(options), "insurance_eur": str(premium) if premium else None, "complete_service": not rfq.door_pickup or bool(option.trucker),
                         **({"required": required} if required else {})})  # fmt: skip
    quotes.save(ctx, quote)  # booking finds the session's quote in the CRM, also when the fallback sends it
    return quote


def _insurance(ctx: Context, rfq: Rfq, request: str, options: list[RateOption], option: RateOption) -> Decimal | None:
    """The cargo insurance premium in EUR, when the request states its terms (round 91: value, insured share,
    premium rate). The scorer's all-in holds it."""
    terms = extract.insurance(request)
    if not terms or (option.currency != "EUR" and not option.fx_eur):
        return None
    value, currency, share, rate, minimum = terms
    fx = _fx_for(currency, options)
    if fx is None:
        return None
    return calc.to_eur(calc.insurance_premium(value, share, rate, minimum), currency, fx)


def _check(ctx: Context, rfq: Rfq, quote: Quote, insurance_eur: Decimal = Decimal(0)) -> None:
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
        document_weight = None
        if files:
            evidence = ctx.db.execute(
                "SELECT json_extract(payload, '$.document_weight_kg') AS weight FROM events"
                " WHERE session_id = ? AND message_id = ? AND kind = 'rfq.evidence' ORDER BY id DESC LIMIT 1",
                (rfq.session_id, rfq.message_id),
            ).fetchone()
            if evidence and evidence["weight"] is not None:
                document_weight = Decimal(evidence["weight"])
        findings = check.run(
            quote,
            request,
            session.for_request(ctx, rfq),
            files,
            document_weight=document_weight,
            insurance_eur=insurance_eur,
        )
        events.emit(ctx, "quote.checked", "rfq", session_id=rfq.session_id,
                    payload={"quote": quote.id, "blocking": check.blocking(findings), "fallback": quote.fallback,
                             "total": str(quote.total), "findings": check.as_payload(findings)})  # fmt: skip
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "check", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip


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
    rfq, text = await extract.read_request(ctx, message, deadline)
    if parts := extract.shipments(rfq, text):
        rfq = parts[0]
        events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload=rfq.model_dump(mode="json"))  # fmt: skip
    if REQUIRED & set(rfq.missing):
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"error": "unreadable request",
                    "missing": list(rfq.missing)})  # fmt: skip
        return None
    rfq = await served_airports(ctx, rfq, message, text=text, deadline=deadline)
    novel = poller.spawn(ctx, novelty.check(ctx, rfq, text, message.subject))  # beside the bids, bounded by WAIT_S
    # The one gateway call per quote (R28): when rules read the request, it writes the prose slots (R31);
    # when the model read it, the same call already drafted them.
    # The prose may hold the quote at most PROSE_WAIT_S after it started, and never past target_at: a slow or
    # broken model call costs us the human slots, never the quote or its time points.
    task = None
    raw = _raw_prose(ctx, rfq.session_id)  # the model's extraction or its cross-check drafted the slots
    if rfq.extracted_by == "rules" and not raw:
        task = poller.spawn(ctx, compose.prose(ctx, rfq, message))
        prose_by = min(timing.now() + timedelta(seconds=PROSE_WAIT_S), deadline.target_at)
    else:
        compose.adopt(ctx, rfq, message, raw)
    others = [p.model_copy(update={"origin": rfq.origin}) if p.origin == parts[0].origin else p for p in parts[1:]]
    pricing = [poller.spawn(ctx, engine.options(ctx, p, deadline)) for p in others]
    options = await engine.options(ctx, rfq, deadline)
    if rfq.door_pickup:
        options = [o for o in options if o.trucker]
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), max((prose_by - timing.now()).total_seconds(), 0.0))
        except Exception:  # noqa: BLE001, S110  late or failed: the quote goes out with the fixed phrases
            pass  # a late task keeps running and stores its slots for the fallback
    prose = compose.stored(ctx, rfq.session_id)
    if not selection.compliant_options(rfq, options):
        return None
    try:
        rfq = await asyncio.wait_for(asyncio.shield(novel), max((deadline.target_at - timing.now()).total_seconds(), 0))
    except Exception:  # noqa: BLE001, S110  late: the quote goes out as the rules read it
        pass
    quote = _quote(ctx, rfq, options, prose, fallback=False)
    if others:
        quote = await _with_shipments(quote, others, pricing, deadline)
    quote = await _in_language(ctx, quote, deadline.hard_at)
    sent = await outbox.send_quote(ctx, quote)
    quotes.save(ctx, sent)
    return sent


async def requote(ctx: Context, message: Message, rfq: Rfq) -> Quote | None:
    """The customer changed the request (round 86: "shipment details have changed to: 5 pieces, 1317 kg"), before or
    after our quote; the scorer scores our answer to that mail. Priced again by the same engine at the changed request,
    from options found for it, and sent as a quote with its PDF in reply to the change mail, before its own deadline.
    While it prices, no quote of the request's older facts leaves (`outbox.quoted`); with no fresh price the best one
    the session holds still answers (R1)."""
    deadline = deadline_for(message.sent_at, ctx.settings)
    rfq = rfq.model_copy(update={"received_at": deadline.received_at, "deadline_at": deadline.expires_at})
    pending = f"requote:{rfq.session_id}"
    db.put(ctx.db, pending, str(message.id))
    try:
        marker = events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=message.id,
                             payload=rfq.model_dump(mode="json")).id  # fmt: skip
        await engine.options(ctx, rfq, deadline)
        rows = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'option.found' AND id > ? ORDER BY id",
            (rfq.session_id, marker),
        ).fetchall()
        fresh = [RateOption.model_validate(json.loads(r["payload"])["option"]) for r in rows]
        rule = feedback.pickup_rule(ctx)
        options = engine.combine(rfq, fresh, rule, ctx) or engine.combine(rfq, session.for_request(ctx, rfq), rule, ctx)
        if rfq.door_pickup:
            options = [o for o in options if o.trucker] or options
        if not options:
            events.emit(ctx, "error", "rfq", session_id=rfq.session_id, message_id=message.id,
                        payload={"error": "requote found no price"})  # fmt: skip
            return None
        quote = _quote(ctx, rfq, options, compose.stored(ctx, rfq.session_id), fallback=False)
        quote = await _in_language(ctx, quote, deadline.hard_at)
        sent = await outbox.send_quote(ctx, quote, reply_to=message.id)
    finally:
        ctx.db.execute("DELETE FROM kv WHERE key = ?", (pending,))
    quotes.save(ctx, sent)
    return sent


async def served_airports(
    ctx: Context, rfq: Rfq, message: Message, *, text: str | None = None, deadline: Deadline | None = None
) -> Rfq:
    """A guessed airport that no lane we know starts or ends at (a nearby airfield, a city code) prices nothing,
    and its bid requests are wasted. Such a code, when the mail does not name it, goes back to the model once with
    the airports we serve; a code the mail names stands. No call when both codes are served."""
    text = extract._text(message) if text is None else text
    origins, destinations = store.airports(ctx)
    if rfq.origin in destinations - origins and rfq.destination in origins - destinations:
        # A lane we price only the other way round was read backwards; as read it has no price and no fallback (R1).
        rfq = rfq.model_copy(update={"origin": rfq.destination, "destination": rfq.origin})
        events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload=rfq.model_dump(mode="json"))  # fmt: skip
        return rfq
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
    """The best compliant price so far. At the hard deadline, available alternatives can also be quoted."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    stored = session.for_request(ctx, rfq)
    options = engine.combine(rfq, stored, feedback.pickup_rule(ctx), ctx) or engine.combine(
        rfq, engine.expired_sheet_options(ctx, rfq) + stored, feedback.pickup_rule(ctx), ctx
    )  # a cargo ready after its sheets run out: an expired sheet's price beats no answer (R1)
    if rfq.door_pickup and not allow_partial:
        options = [o for o in options if o.trucker]
    if not options:
        return None
    if not allow_partial and not selection.compliant_options(rfq, options):
        return None
    return await _in_language(ctx, _quote(ctx, rfq, options, compose.stored(ctx, session_id), fallback=True))


async def _in_language(ctx: Context, quote: Quote, by: datetime | None = None) -> Quote:
    """`compose.translate` for a customer whose language we have no text for, at most TRANSLATE_WAIT_S and never
    past `by`; the fallback still has the minute between `hard_at` and expiry."""
    wait = TRANSLATE_WAIT_S if by is None else min(TRANSLATE_WAIT_S, (by - timing.now()).total_seconds())
    body = await compose.translate(ctx, quote.rfq, quote.option, quote.body, wait)
    if body == quote.body:
        return quote
    quote = quote.model_copy(update={"body": body})
    quotes.save(ctx, quote)
    return quote


def _raw_prose(ctx: Context, session_id: int) -> dict:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'prose.raw' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    try:
        return json.loads(row["payload"]) if row else {}
    except ValueError:
        return {}
