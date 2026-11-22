import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import check, compose, dangerous, extract, gaps, novelty, repair, unmet
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import db, events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline, deadline_for, out_of_scope
from pixel.crm import quotes
from pixel.pricing import calc, engine, pickup, portal_rates, selection, session, store

REQUIRED = {"origin", "destination", "weight_kg"}
PROSE_WAIT_S = 10
TRANSLATE_WAIT_S = 10
REPAIR_MARGIN_S = 15  # repairs end this long before the orchestrator takes the request over (`Deadline.recovery_at`)
FALLBACK_REPAIR_S = 15  # what the deadline fallback may spend on repairs
REPRICE_PORTAL_S = 8.0  # a corrected reading reads the portal again for at most this long
FOLLOWUP_WAIT_S = 5.0  # how long a quote waits for a newer customer mail in its thread to be read
FOLLOWUP_POLL_S = 0.5
SECOND_READ_CLOSE = Decimal("0.01")  # the prose call's figure agrees within this share of the rules' figure
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


async def _with_shipments(
    ctx: Context, quote: Quote, others: list[Rfq], pricing: list[asyncio.Task], deadline: Deadline
) -> Quote:
    """The mail's further shipments, each priced and selected like the first, in the same quote (R4). A pricing task
    still running at `target_at` gives the prices it stored so far (round 150: TLV's were all in at 30 s, its task
    waited on a rate read for the next break, and TLV was left out: one_of_two). One without a price is left out, and
    with it the sum of all."""
    wait = max((deadline.target_at - timing.now()).total_seconds(), 0.0)
    done, _ = await asyncio.wait(pricing, timeout=wait)
    priced = []
    for rfq, task in zip(others, pricing, strict=True):
        if task in done and not task.exception():
            found = task.result()
        else:
            found = engine.combine(rfq, session.for_request(ctx, rfq), feedback.pickup_rule(ctx), ctx)
        options = [o for o in found if o.trucker or not rfq.door_pickup]
        if not options or (rfq.required_carriers and not selection.requested_options(rfq, options)):
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


def _check(ctx: Context, rfq: Rfq, quote: Quote, insurance_eur: Decimal = Decimal(0)) -> list[check.Finding]:
    """The look at the drafted quote (`check.py`): a `quote.checked` event with what it found, the findings returned.
    It never changes the quote; a BLOCK finding is an issue for `feedback.alert`, so a person sees it at once, and
    `repair.settle` looks again at the text that leaves. PIXEL_PRECHECK=off skips it. Never raises."""
    if ctx.settings.precheck == "off":
        return []
    try:
        findings = _findings(ctx, rfq, quote, insurance_eur)
        if compose.language_of(rfq) != rfq.language:  # the draft is English until `_in_language` translates it
            findings = [f for f in findings if f.rule != "language_sent"]
        events.emit(ctx, "quote.checked", "rfq", session_id=rfq.session_id,
                    payload={"quote": quote.id, "blocking": check.blocking(findings), "fallback": quote.fallback,
                             "total": str(quote.total), "findings": check.as_payload(findings)})  # fmt: skip
        return findings
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "check", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip
        return []


def _findings(ctx: Context, rfq: Rfq, quote: Quote, insurance_eur: Decimal = Decimal(0)) -> list[check.Finding]:
    """`check.run` on `quote` with what the session knows: the customer's mail, its files, the corroborated
    document weight and every option found."""
    request, files = repair.request(ctx, rfq)
    document_weight = None
    if files:
        evidence = ctx.db.execute(
            "SELECT json_extract(payload, '$.document_weight_kg') AS weight FROM events"
            " WHERE session_id = ? AND message_id = ? AND kind = 'rfq.evidence' ORDER BY id DESC LIMIT 1",
            (rfq.session_id, rfq.message_id),
        ).fetchone()
        if evidence and evidence["weight"] is not None:
            document_weight = Decimal(evidence["weight"])
    return check.run(
        quote,
        request,
        session.for_request(ctx, rfq),
        files,
        document_weight=document_weight,
        insurance_eur=insurance_eur,
    )


def _premium(ctx: Context, quote: Quote) -> Decimal:
    """The insurance premium in EUR `_quote` added to this quote, from its `quote.drafted` event."""
    row = ctx.db.execute(
        "SELECT json_extract(payload, '$.insurance_eur') AS premium FROM events WHERE session_id = ?"
        " AND kind = 'quote.drafted' AND json_extract(payload, '$.quote_id') = ? ORDER BY id DESC LIMIT 1",
        (quote.session_id, quote.id),
    ).fetchone()
    return Decimal(row["premium"]) if row and row["premium"] else Decimal(0)


async def _settled(
    ctx: Context,
    rfq: Rfq,
    options: list[RateOption],
    prose: compose.Prose | None,
    *,
    fallback: bool,
    translate_by: datetime | None,
    repair_by: datetime | None,
    extend: Callable[[Quote], Awaitable[Quote]] | None = None,
    later: bool = False,
) -> Quote:
    """Draft, `extend` (the mail's further shipments), translate, then `repair.settle`: the check runs on the text
    that leaves, and a BLOCK it can mend is mended before `repair_by`, every rebuild extended the same way. The quote
    returned is the one the CRM holds. `later`: the figures are a later mail's (a requote), so no reading repair."""

    async def draft_of(rfq: Rfq, options: list[RateOption], prose: compose.Prose | None, by: datetime | None) -> Quote:
        quote = _quote(ctx, rfq, options, prose, fallback)
        return await _in_language(ctx, await extend(quote) if extend else quote, by)

    async def build(draft: repair.Draft) -> Quote:
        by = translate_by if repair_by is None else min(filter(None, (translate_by, repair_by)))
        return await draft_of(draft.rfq, draft.options, draft.prose, by)

    quote = await draft_of(rfq, options, prose, translate_by)
    settled = await repair.settle(
        ctx,
        quote,
        repair.Draft(rfq, list(options), prose),
        build,
        lambda q: _findings(ctx, q.rfq, q, _premium(ctx, q)),
        by=repair_by,
        reprice=None
        if later or _later_figures(ctx, rfq)
        else lambda changed: _repriced(ctx, changed, deadline_for(changed.received_at, ctx.settings), repair_by),
    )
    quotes.save(ctx, settled)
    return settled


async def _repriced(ctx: Context, rfq: Rfq, deadline: Deadline, by: datetime | None) -> list[RateOption]:
    """The request priced again after a repair corrected its weight or volume: the sheets at once, the portal for at
    most REPRICE_PORTAL_S and never past `by`, the pickups found so far. Airline bids were given for the old weight
    and take no part; with no sheet or portal price the repair has nothing to offer."""
    found = engine.sheet_options(ctx, rfq)
    wait = REPRICE_PORTAL_S if by is None else min(REPRICE_PORTAL_S, (by - timing.now()).total_seconds())
    if ctx.portal is not None and wait > 0:
        try:
            found += await asyncio.wait_for(portal_rates.options(ctx, rfq, deadline), wait)
        except Exception as exc:  # noqa: BLE001  the sheets still price it
            events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                        payload={"where": "repair:reprice", "error": type(exc).__name__})  # fmt: skip
    pickups = [o for o in session.for_request(ctx, rfq) if o.trucker]
    options = engine.combine(rfq, found + pickups, feedback.pickup_rule(ctx), ctx)
    return [o for o in options if o.trucker] if rfq.door_pickup else options


def _later_figures(ctx: Context, rfq: Rfq) -> bool:
    """Whether the request's figures may come from a later mail than the one the check reads (`repair.request`): an
    answer to our question (round 104) or a customer's change (round 86). The check still reads; a reading repair
    would set the old figures back."""
    return bool(
        ctx.db.execute(
            "SELECT 1 FROM messages a JOIN messages q ON q.id = a.in_reply_to WHERE a.session_id = ?"
            " AND a.direction = 'in' AND q.purpose = 'question' LIMIT 1",
            (rfq.session_id,),
        ).fetchone()
        or ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND kind = 'followup.read'"
            " AND json_extract(payload, '$.changes') NOT IN ('{}', '') LIMIT 1",
            (rfq.session_id,),
        ).fetchone()
    )


def _unread_followups(ctx: Context, rfq: Rfq) -> list[int]:
    """The customer's mails in this thread newer than the request that no handler has finished (`mail_work`), not
    counting an answer to our question, which `extract._ask` reads itself (rounds 86 and 104: a later mail that
    changed the request went unread)."""
    rows = ctx.db.execute(
        "SELECT m.id FROM messages m LEFT JOIN mail_work w ON w.message_id = m.id WHERE m.session_id = ?"
        " AND m.direction = 'in' AND m.id > ? AND lower(m.sender) = lower(?) AND COALESCE(w.completed, 0) = 0"
        " AND NOT EXISTS (SELECT 1 FROM messages q WHERE q.id = m.in_reply_to AND q.purpose = 'question')",
        (rfq.session_id, rfq.message_id, rfq.customer),
    ).fetchall()
    return [r["id"] for r in rows]


async def _followup_wait(ctx: Context, rfq: Rfq, until: datetime) -> None:
    """A newer customer mail in the thread may change what we quote: wait at most FOLLOWUP_WAIT_S, never past `until`,
    for its handler (`booking.handle_reply` prices a change again and answers it, `outbox.quoted` then holds this
    quote back). Still unread after that: a `quote.followup_wait` event, which a person sees; the quote goes out."""
    if not (unread := _unread_followups(ctx, rfq)):
        return
    loop = asyncio.get_running_loop()
    end = loop.time() + max(0.0, min(FOLLOWUP_WAIT_S, (until - timing.now()).total_seconds()))
    while (left := _unread_followups(ctx, rfq)) and not outbox.quoted(ctx, rfq.session_id) and loop.time() < end:
        await asyncio.sleep(min(FOLLOWUP_POLL_S, max(0.0, end - loop.time())))
    events.emit(ctx, "quote.followup_wait", "rfq", session_id=rfq.session_id,
                payload={"unread": unread, "still_unread": left, "superseded": outbox.quoted(ctx, rfq.session_id)})  # fmt: skip


def _agrees(read: object, rules: Decimal | str) -> bool:
    if isinstance(rules, str):
        return str(read).strip().upper() == rules
    try:
        value = Decimal(str(read))
    except (ArithmeticError, ValueError):
        return True  # no figure: nothing to disagree with
    return abs(value - rules) <= max(Decimal("0.01"), abs(rules) * SECOND_READ_CLOSE)


async def _second_read(ctx: Context, rfq: Rfq, message: Message, text: str, deadline: Deadline) -> Rfq:
    """The prose call, which runs on every request the rules read, also reads the destination, weight and volume
    (`compose.prose`). It is never taken itself: where it disagrees with the rules, the larger model reads the
    request (`extract._crosscheck`), whose weight or volume wins only when code reads that figure as a gross total
    with its unit. A changed reading is priced again (`_repriced`); with no price for it the rules' reading stands."""
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.second_read' ORDER BY id DESC LIMIT 1",
        (rfq.session_id,),
    ).fetchone()
    figures = (json.loads(row["payload"]).get("figures") or {}) if row else {}
    pairs = (("weight_kg", rfq.weight_kg), ("volume_cbm", rfq.volume_cbm))  # what a second reading can change
    dissent = [f for f, ours in pairs if figures.get(f) not in (None, "", 0) and not _agrees(figures[f], ours)]
    if not dissent:
        return rfq
    events.emit(ctx, "rfq.second_read", "rfq", session_id=rfq.session_id,
                payload={"dissent": dissent, "rules": {f: str(v) for f, v in pairs}})  # fmt: skip
    if ctx.settings.second_read != "on" or not ctx.settings.model_check:
        return rfq
    marker = events.emit(ctx, "rfq.second_read", "rfq", session_id=rfq.session_id, payload={"escalated": True}).id
    read, _ = await extract._crosscheck(ctx, rfq, message, text, [("mail", text)], deadline)
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.crosscheck' AND id > ? ORDER BY id DESC LIMIT 1",
        (rfq.session_id, marker),
    ).fetchone()
    differs = (json.loads(row["payload"]).get("differs") or {}) if row else {}
    # Only the cargo figures, and only one code reads in the customer's own words too (a gross total `_crosscheck`
    # took, or a plain reading `check` makes: one figure, figures times pieces, lots added up): on a mail alone
    # the rules' wish and pieces are tested best.
    own = extract.own_words(text)
    update = {}
    for field, readings in (
        ("weight_kg", check.weight_readings(own)),
        ("volume_cbm", check.volume_readings(own, rfq.pieces)),
    ):
        model = getattr(read, field) if getattr(read, field) != getattr(rfq, field) else None
        if model is None and field in differs:
            try:
                model = Decimal(differs[field][1])
            except (ArithmeticError, ValueError, IndexError):
                model = None
        if model is not None and any(_agrees(model, r) for r in readings):
            update[field] = next(r for r in readings if _agrees(model, r))
    return rfq.model_copy(update=update)


async def _read_again(
    ctx: Context, rfq: Rfq, options: list[RateOption], message: Message, text: str, deadline: Deadline, by: datetime
) -> tuple[Rfq, list[RateOption]]:
    """`_second_read`, and the request priced again when it changed the weight or volume; the airline prices for
    the old chargeable weight are left out from then on (`option.vetoed`), so the fallback prices the new one."""
    read = await _second_read(ctx, rfq, message, text, deadline)
    if (read.weight_kg, read.volume_cbm) == (rfq.weight_kg, rfq.volume_cbm):
        return rfq, options
    fresh = await _repriced(ctx, read, deadline, by)
    if not fresh:
        return rfq, options
    old_kg, new_kg = (
        calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm),
        calc.chargeable_kg(read.weight_kg, read.volume_cbm),
    )
    if old_kg != new_kg:
        events.emit(ctx, "option.vetoed", "rfq", session_id=rfq.session_id,
                    payload={"kind": "chargeable", "kg": str(old_kg), "rule": "second_read"})  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                payload=read.model_dump(mode="json"))  # fmt: skip
    return read, fresh


async def _close_gaps(
    ctx: Context,
    rfq: Rfq,
    options: list[RateOption],
    deadline: Deadline,
    text: str,
    repair_by: datetime | None = None,
    subject: str = "",
) -> tuple[Rfq, list[RateOption]]:
    """`gaps.close` (a failed portal read read again, a pending answer waited for, an unread sentence mapped again),
    then the request priced again from every option the session holds when anything new came in. `PIXEL_GAPS=log`
    only records the gaps."""
    if ctx.settings.gaps == "log":
        found = gaps.find(ctx, rfq)
        if found:
            events.emit(ctx, "quote.gaps", "rfq", session_id=rfq.session_id,
                        payload={"found": [g.__dict__ for g in found], "open": [g.__dict__ for g in found],
                                 "logged_only": True})  # fmt: skip
        return rfq, options
    outage_by = repair_by or _repair_by(deadline)
    rfq, changed = await gaps.close(ctx, rfq, deadline, by=min(gaps.by_for(deadline), outage_by), outage_by=outage_by,
                                    text=text, subject=subject)  # fmt: skip
    if not changed:
        return rfq, options
    fresh = engine.combine(rfq, session.for_request(ctx, rfq), feedback.pickup_rule(ctx), ctx)
    fresh = [o for o in fresh if o.trucker] if rfq.door_pickup else fresh
    return (rfq, fresh) if fresh and selection.compliant_options(rfq, fresh) else (rfq, options)


def _repair_by(deadline: Deadline) -> datetime:
    """Repairs end REPAIR_MARGIN_S before the orchestrator takes over, or before `hard_at` for a request first seen
    after that (`orchestrator.quote_with_deadline` then keeps its handler until `hard_at`)."""
    margin = timedelta(seconds=REPAIR_MARGIN_S)
    soon = deadline.recovery_at - margin
    return soon if timing.now() < soon else deadline.hard_at - margin


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
    if out_of_scope(ctx, rfq.session_id):  # out of scope: no airline asked, the orchestrator declines at once
        return None
    novel = poller.spawn(ctx, novelty.check(ctx, rfq, text, message.subject, extract._text(message)))  # within WAIT_S
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
    # Only a required airline is worth waiting for: a late bid of its own can still come. Bids were collected until
    # `target_at`, so a later price meeting a missed arrival date is unlikely: that quote goes now and names the
    # alternative (`novelty.with_note`).
    if rfq.required_carriers and not selection.requested_options(rfq, options):
        return None
    try:
        rfq = await asyncio.wait_for(asyncio.shield(novel), max((deadline.target_at - timing.now()).total_seconds(), 0))
    except Exception:  # noqa: BLE001, S110  late: the quote goes out as the rules read it
        pass
    repair_by = _repair_by(deadline)  # once: a wait below must not move it past the orchestrator's takeover
    if ctx.settings.gaps != "off" and options and not others:  # no price at all: the fallback waits for one
        rfq, options = await _close_gaps(ctx, rfq, options, deadline, text, repair_by, message.subject)
    if task is not None and task.done() and not others and ctx.settings.second_read != "off":
        rfq, options = await _read_again(ctx, rfq, options, message, text, deadline, repair_by)
    await _followup_wait(ctx, rfq, repair_by)
    extend = (lambda q: _with_shipments(ctx, q, others, pricing, deadline)) if others else None
    quote = await _settled(ctx, rfq, options, prose, fallback=False, translate_by=deadline.hard_at,
                           repair_by=repair_by, extend=extend)  # fmt: skip
    await unmet.settle(ctx, quote, text, deadline, repair_by)
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
        quote = await _settled(ctx, rfq, options, compose.stored(ctx, rfq.session_id), fallback=False,
                               translate_by=deadline.hard_at, repair_by=deadline.hard_at, later=True)  # fmt: skip
        sent = await outbox.send_quote(ctx, quote, reply_to=message.id)
    finally:
        ctx.db.execute("DELETE FROM kv WHERE key = ?", (pending,))
    quotes.save(ctx, sent)
    return sent


IMPORT = re.compile(r"\b(?:import\w*|einfuhr)\b", re.IGNORECASE)


def _import_said(rfq: Rfq, text: str) -> bool:
    """The mail calls it an import and writes its lane this way round: "air import from NRT to HAM" (round 159) is
    out of scope (R36), never a lane read backwards."""
    return bool(IMPORT.search(text)) and extract._lane(text) == (rfq.origin, rfq.destination)


async def served_airports(
    ctx: Context, rfq: Rfq, message: Message, *, text: str | None = None, deadline: Deadline | None = None
) -> Rfq:
    """A guessed airport that no lane we know starts or ends at (a nearby airfield, a city code) prices nothing,
    and its bid requests are wasted. Such a code, when the mail does not name it, goes back to the model once with
    the airports we serve; a code the mail names stands. No call when both codes are served."""
    text = extract._text(message) if text is None else text
    origins, destinations = store.airports(ctx)
    if (
        rfq.origin in destinations - origins
        and rfq.destination in origins - destinations
        and not _import_said(rfq, text)
    ):
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
    """The best price so far, from the required airline while one can still answer. At the hard deadline another
    airline's price can also be quoted."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return None
    stored = session.for_request(ctx, rfq)
    if timing.now() >= deadline_for(rfq.received_at, ctx.settings).target_at:  # the truckers had their time
        stored += pickup.estimates(ctx, rfq, stored)
    options = engine.combine(rfq, stored, feedback.pickup_rule(ctx), ctx) or engine.combine(
        rfq, engine.expired_sheet_options(ctx, rfq) + stored, feedback.pickup_rule(ctx), ctx
    )  # a cargo ready after its sheets run out: an expired sheet's price beats no answer (R1)
    if rfq.door_pickup and not allow_partial:
        options = [o for o in options if o.trucker]
    if not options:
        return None
    if rfq.required_carriers and not allow_partial and not selection.requested_options(rfq, options):
        return None
    return await _settled(ctx, rfq, options, compose.stored(ctx, session_id), fallback=True, translate_by=None,
                          repair_by=timing.now() + timedelta(seconds=FALLBACK_REPAIR_S))  # fmt: skip


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
