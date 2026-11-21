import asyncio
import json
import os
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import check, compose, dangerous, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import db, events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import calc, engine, portal_rates, selection, session, store

REQUIRED = {"origin", "destination", "weight_kg"}
HEAL_FIELDS = ("weight_kg", "volume_cbm", "origin", "destination", "door_pickup")
HEAL_RULES = {
    "weight", "weight_unexplained", "weight_differs", "chargeable", "chargeable_below_volume",
    "per_kg", "per_kg_high", "per_kg_low", "pickup", "door_no_pickup",
}  # fmt: skip
HEAL_MIN_S = 15
HEAL_SEND_RESERVE_S = 3
events.KINDS += ("quote.heal_attempted", "quote.healed")
PROSE_WAIT_S = 10
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


def _usage(ctx: Context, session_id: int) -> dict:
    calls = ctx.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'llm.call' AND session_id = ?", (session_id,)
    ).fetchone()[0]
    return {"llm_calls": calls, "llm_cost_usd": llm.spend_usd(ctx, session_id=session_id)}


def _draft(ctx: Context, rfq: Rfq, options: list[RateOption], prose: compose.Prose | None, fallback: bool) -> Quote:
    option = selection.select(rfq, options)
    if rfq.currency and option.currency != rfq.currency:
        option = calc.in_currency(option, rfq.currency, _fx_for(rfq.currency, options))
    subject, body = compose.render(
        rfq,
        option,
        prose,
        compose.customer_subject(ctx, rfq.message_id),
        ctx.settings.signer,
        compose.customer_body(ctx, rfq.message_id),
    )
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
        created_at=timing.now(),
        **_usage(ctx, rfq.session_id),
    )
    return _noted(ctx, rfq, quote)


def _save_draft(ctx: Context, quote: Quote, options: list[RateOption]) -> None:
    option = quote.option
    events.emit(ctx, "quote.drafted", "rfq", session_id=quote.session_id,
                payload={"quote_id": quote.id, "option": option.model_dump(mode="json"), "fallback": quote.fallback,
                         "options": len(options), "complete_service": not quote.rfq.door_pickup or bool(option.trucker)})  # fmt: skip
    quotes.save(ctx, quote)  # booking finds the session's quote in the CRM, also when the fallback sends it


def _quote(ctx: Context, rfq: Rfq, options: list[RateOption], prose: compose.Prose | None, fallback: bool) -> Quote:
    quote = _draft(ctx, rfq, options, prose, fallback)
    _check(ctx, rfq, quote)
    _save_draft(ctx, quote, options)
    return quote


def _findings(ctx: Context, rfq: Rfq, quote: Quote, options: list[RateOption]) -> list[check.Finding]:
    row = ctx.db.execute("SELECT data FROM messages WHERE id = ?", (rfq.message_id,)).fetchone()
    message = Message.model_validate_json(row["data"]) if row else None
    request = extract._text(message) if message else ""
    files = tuple(a.filename for a in message.attachments) if message else ()
    return check.run(quote, request, options, files)


def _record_check(ctx: Context, quote: Quote, findings: list[check.Finding]) -> None:
    events.emit(ctx, "quote.checked", "rfq", session_id=quote.session_id,
                payload={"quote": quote.id, "blocking": check.blocking(findings), "fallback": quote.fallback,
                         "total": str(quote.total), "findings": check.as_payload(findings)})  # fmt: skip


def _check(ctx: Context, rfq: Rfq, quote: Quote) -> list[check.Finding]:
    """Check and record the draft. An unavailable check cannot hold the quote."""
    if ctx.settings.precheck == "off":
        return []
    try:
        findings = _findings(ctx, rfq, quote, session.for_request(ctx, rfq))
        _record_check(ctx, quote, findings)
        return findings
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        with suppress(Exception):
            events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                        payload={"where": "check", "error": type(exc).__name__})  # fmt: skip
        return []


def _noted(ctx: Context, rfq: Rfq, quote: Quote) -> Quote:
    """The quote with the dangerous-goods note, when the request's documents declare some (`dangerous.py`)."""
    found = dangerous.declared(ctx, rfq)
    if not found:
        return quote
    goods, source = found
    return quote.model_copy(update={"body": dangerous.with_note(quote.body, goods, compose.language_of(rfq), source)})


def _heal_attempt(ctx: Context, session_id: int) -> bool:
    return (
        ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='quote.heal_attempted' LIMIT 1", (session_id,)
        ).fetchone()
        is not None
    )


def _heal_request(ctx: Context, rfq: Rfq, model: Rfq, text: str) -> Rfq:
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id=? AND message_id=? AND kind='rfq.evidence' ORDER BY id DESC LIMIT 1",
        (rfq.session_id, rfq.message_id),
    ).fetchone()
    evidence = json.loads(row["payload"]) if row else {}
    sources = evidence.get("sources", {})
    preserved = {field for field in (*HEAL_FIELDS, "pieces") if any(s != "mail" for s in sources.get(field, ()))}
    if evidence.get("document_weight_kg"):
        preserved.add("weight_kg")
    if rfq.pickup_zone and rfq.origin:
        preserved.add("origin")
    if extract.OWN_DELIVERY.search(text) or extract.DOOR.search(text) or rfq.pickup_zone:
        preserved.add("door_pickup")
    for field in ("origin", "destination"):
        if extract.names_code(text, getattr(rfq, field)):
            preserved.add(field)
    healed = rfq.model_copy(update={field: getattr(model, field) for field in (*HEAL_FIELDS, "pieces")
                                    if field not in preserved} | {"extracted_by": "model"})  # fmt: skip
    if healed.door_pickup and not healed.pickup_zone:
        if model.pickup_postcode and model.pickup_postcode in extract.POSTCODE.findall(text):
            healed = healed.model_copy(update={"pickup_postcode": model.pickup_postcode,
                                               "pickup_zone": model.pickup_zone})  # fmt: skip
        healed = extract._town_zone(ctx, healed, text, guess=False)
    healed = extract._zone_origin(ctx, healed, text)
    missing = set(rfq.missing) | set(model.missing)
    return healed.model_copy(update={"missing": tuple(m for m in sorted(missing)
                                if not getattr(healed, m, None) and not (m == "pickup_postcode" and healed.pickup_zone))})  # fmt: skip


def _priced_for(rfq: Rfq, option: RateOption) -> bool:
    # The engine can reuse offers from the same lane. A rate for a different chargeable weight cannot heal it.
    needed = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    return session.on_lane(option, rfq) and abs(option.chargeable_kg - needed) <= Decimal("0.01")


async def _heal(
    ctx: Context, message: Message, text: str, deadline: Deadline, original: Quote,
    findings: list[check.Finding], prose: compose.Prose | None,
) -> Quote:  # fmt: skip
    rfq = original.rfq
    target = min(deadline.target_at, deadline.hard_at)
    remaining = (target - timing.now()).total_seconds()
    if rfq.extracted_by != "rules" or os.environ.get("PIXEL_SELF_HEAL", "on") == "off" or remaining < HEAL_MIN_S:
        return original
    blocks = [f.rule for f in findings if f.level == check.BLOCK]
    if not HEAL_RULES.intersection(blocks):
        return original
    try:
        with db.atomic(ctx.db):
            if _heal_attempt(ctx, rfq.session_id):
                return original
            events.emit(ctx, "quote.heal_attempted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                        payload={"quote": original.id, "blocking": blocks})  # fmt: skip
        cutoff = target - timedelta(seconds=HEAL_SEND_RESERVE_S)
        retry_deadline = replace(deadline, target_at=cutoff, hard_at=cutoff)
        async with asyncio.timeout(max(0.0, (cutoff - timing.now()).total_seconds())):
            result = await asyncio.wait_for(
                llm.ask_json(ctx, purpose="rfq.heal", system=extract.SYSTEM, schema=extract.SCHEMA,
                             user=text, session_id=rfq.session_id, max_output_tokens=500),
                timeout=min(8.0, remaining / 2),
            )  # fmt: skip
            combined = message.model_copy(update={"subject": "", "body": text})
            healed = _heal_request(ctx, rfq, extract._from_model(result.data, combined, deadline), text)
            if REQUIRED.intersection(healed.missing) or not any(
                getattr(healed, key) != getattr(rfq, key) for key in HEAL_FIELDS
            ):
                return original
            healed = await served_airports(ctx, healed, message, text=text, deadline=retry_deadline, publish=False)
            options = [
                o for o in await engine.options(ctx, healed, retry_deadline)
                if _priced_for(healed, o) and (not healed.door_pickup or o.trucker)
            ]  # fmt: skip
            if not options or timing.now() >= cutoff:
                return original
            candidate = _draft(ctx, healed, options, prose, fallback=False)
            checked_options = [o for o in session.for_request(ctx, healed) if _priced_for(healed, o)]
            after = _findings(ctx, healed, candidate, checked_options)
            if (
                check.blocking(after)
                or any(f.detail.startswith("check failed:") for f in after)
                or timing.now() >= cutoff
            ):
                return original
        with db.atomic(ctx.db):
            _record_check(ctx, candidate, after)
            _save_draft(ctx, candidate, options)
            events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                        payload=healed.model_dump(mode="json"))  # fmt: skip
            events.emit(ctx, "quote.healed", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                        payload={"before": {"quote": original.id, "blocking": blocks, "total": str(original.total)},
                                 "after": {"quote": candidate.id, "blocking": [], "total": str(candidate.total)}})  # fmt: skip
        return candidate
    except Exception as exc:  # noqa: BLE001  the draft remains usable when the optional retry fails
        with suppress(Exception):
            events.emit(ctx, "error", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                        payload={"where": "rfq.heal", "error": type(exc).__name__})  # fmt: skip
        return original


async def handle(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """extract -> pricing.engine.options (emits `option.found` as they arrive) -> selection.select ->
    compose.render -> outbox.send_quote. Sends at `deadline.target_at` at the latest unless waiting can
    still change the answer and `hard_at` is not reached."""
    # During extraction only, the outbox's dry-run guard holds the optional question.
    extracting = ctx if feedback.ask_allowed(ctx) else replace(ctx, settings=replace(ctx.settings, dry_run=True))
    rfq, text = await extract.read_request(extracting, message, deadline)
    if REQUIRED & set(rfq.missing):
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"error": "unreadable request",
                    "missing": list(rfq.missing)})  # fmt: skip
        return None
    rfq = await served_airports(ctx, rfq, message, text=text, deadline=deadline)
    # The one gateway call per quote (R28): when rules read the request, it writes the prose slots (R31);
    # when the model read it, the same call already drafted them.
    # The prose may hold the quote at most PROSE_WAIT_S after it started, and never past target_at: a slow or
    # broken model call costs us the human slots, never the quote or its time points.
    task = None
    if rfq.extracted_by == "rules":
        task = poller.spawn(ctx, compose.prose(ctx, rfq, message))
        prose_by = min(timing.now() + timedelta(seconds=PROSE_WAIT_S), deadline.target_at)
    else:
        compose.adopt(ctx, rfq, message, _raw_prose(ctx, rfq.session_id))
    options = await engine.options(ctx, rfq, deadline)
    if rfq.door_pickup:
        options = [o for o in options if o.trucker]
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), max((prose_by - timing.now()).total_seconds(), 0.0))
        except Exception:  # noqa: BLE001, S110  late or failed: the quote goes out with the fixed phrases
            pass  # a late task keeps running and stores its slots for the fallback
    prose = compose.stored(ctx, rfq.session_id)
    if not options:
        return None
    draft = _draft(ctx, rfq, options, prose, fallback=False)
    findings = _check(ctx, rfq, draft)
    _save_draft(ctx, draft, options)
    chosen = await _heal(ctx, message, text, deadline, draft, findings, prose)
    if chosen is draft and findings and _heal_attempt(ctx, rfq.session_id):
        chosen = draft.model_copy(update=_usage(ctx, rfq.session_id))
    sent = await outbox.send_quote(ctx, chosen)
    quotes.save(ctx, sent)
    return sent


async def served_airports(
    ctx: Context, rfq: Rfq, message: Message, *, text: str | None = None, deadline: Deadline | None = None,
    publish: bool = True,
) -> Rfq:  # fmt: skip
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
    portal_error = None
    try:
        for lane in await asyncio.wait_for(portal_rates.lanes(ctx), timeout=available / 4):
            origins.add(lane.origin)
            destinations.add(lane.destination)
    except Exception as exc:  # noqa: BLE001  the local tariffs and carrier list still apply
        portal_error = type(exc).__name__
    try:
        wrong = {key: served for key, served in wrong.items() if served and getattr(rfq, key) not in served}
        if not wrong:
            return rfq
        user = (f"{text}\n\nRead: {rfq.origin} to {rfq.destination}.\nDeparture airports we serve: "
                f"{' '.join(sorted(origins))}\nDestination airports we serve: {' '.join(sorted(destinations))}")  # fmt: skip
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
    finally:
        # A blocking SQLite write can eat the remaining validation time after a portal timeout.
        if portal_error is not None:
            events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                        payload={"where": "served_airports", "error": portal_error})  # fmt: skip
    update = {k: code for k, served in wrong.items() if (code := str(result.data.get(k) or "").upper()) in served}
    if not update:
        return rfq
    rfq = rfq.model_copy(update=update)
    if publish:
        events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload=rfq.model_dump(mode="json"))  # fmt: skip
    return rfq


async def fallback(ctx: Context, session_id: int, *, allow_partial: bool = False) -> Quote | None:
    """The best complete price so far. At the hard deadline an air-only price can cover a missing pickup."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    stored = session.for_request(ctx, rfq)
    attempted = _heal_attempt(ctx, session_id)
    if attempted:
        stored = [o for o in stored if _priced_for(rfq, o)]
    options = engine.combine(rfq, stored, feedback.pickup_rule(ctx))
    if rfq.door_pickup and not allow_partial:
        options = [o for o in options if o.trucker]
    if not options:
        if attempted and (draft := quotes.for_session(ctx, session_id)) and draft.rfq == rfq:
            return draft.model_copy(update={"fallback": True})
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
