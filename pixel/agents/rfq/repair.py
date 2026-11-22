"""Verify, repair, check again: a quote with a BLOCK finding is mended inside the round, before it leaves.

Almost every round we lost was the first of its kind, and the fix came a round later. `check.py` sees many of those
faults the moment the quote is drafted; this module acts on them. After the quote is rendered and translated,
`settle` runs the check on the very text that would leave. For each BLOCK it has a repair for, it builds the quote
again with that one thing changed, and keeps the new quote only when its check blocks on fewer rules and on no rule
the old one passed (the monotony guard): a repair never makes a quote worse by the check's own measure. At most
`MAX_STEPS` repairs, never past `by`; whatever is best by then is sent (R1).

The repairs are code and make no model call of their own: an option the check distrusts or the customer ruled out
is left out and the quote selected again (`option.vetoed`, which `pricing/session.py` honours, so the deadline
fallback inherits it); a mail in the wrong language is rendered in the customer's (our own text, else the checked
translation of `compose.translate`); a garbled total or leftover is rendered with the fixed phrases; a
dangerous-goods note a translation dropped is put back. Each attempt is a `quote.repaired` event; the final check
is a `quote.checked` event with `stage: "sent"`. `PIXEL_REPAIR`: `on` (default), `detect` (check the text that
leaves, change nothing), `off`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import check, compose, dangerous, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.pricing import calc, engine, selection, session

MAX_STEPS = 3
TRANSLATE_MIN_S = 3.0  # a translation needs this much time left to be worth a try


@dataclass(frozen=True)
class Draft:
    """What a quote is built from; a repair changes one of these and the quote is built again."""

    rfq: Rfq
    options: list[RateOption]
    prose: compose.Prose | None
    vetoed: tuple[dict, ...] = ()  # `session.veto` records


Build = Callable[[Draft], Awaitable[Quote]]
Inspect = Callable[[Quote], list[check.Finding] | None]
Reprice = Callable[[Rfq], Awaitable[list[RateOption]]]
Repaired = tuple[Quote, Draft] | None


@dataclass(frozen=True)
class Tools:
    """What a repair may use: build the quote again, price a changed request again, and the time it has."""

    build: Build
    by: datetime | None
    reprice: Reprice | None = None
    rule: str = ""  # the BLOCK being repaired


async def settle(
    ctx: Context,
    quote: Quote,
    draft: Draft,
    build: Build,
    inspect: Inspect,
    *,
    by: datetime | None,
    reprice: Reprice | None = None,
) -> Quote:
    """The quote to send: `quote` itself, or a repaired one whose check blocks less. Never raises."""
    mode = ctx.settings.repair
    if mode == "off" or ctx.settings.precheck == "off":
        return quote
    try:
        findings = inspect(quote) or []
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        _error(ctx, quote, "inspect", exc)
        return quote
    steps: list[dict] = []
    tried: set[str] = set()
    while mode == "on" and check.blocking(findings) and len(tried) < MAX_STEPS and _time_left(by) > 0:
        rule = next((f.rule for f in findings if f.level == check.BLOCK and f.rule in REPAIRS and f.rule not in tried),
                    None)  # fmt: skip
        if rule is None:
            break
        tried.add(rule)
        try:
            candidate = await REPAIRS[rule](ctx, quote, draft, Tools(build, by, reprice, rule))
            if candidate is None:
                continue
            new_quote, new_draft = candidate
            new_findings = inspect(new_quote) or []
        except Exception as exc:  # noqa: BLE001  a failed repair keeps the quote as it was
            _error(ctx, quote, rule, exc)
            continue
        kept = _better(findings, new_findings)
        events.emit(ctx, "quote.repaired", "rfq", session_id=quote.session_id,
                    payload={"rule": rule, "kept": kept, "quote": new_quote.id, "total_before": str(quote.total),
                             "total_after": str(new_quote.total), "before": _blocks(findings),
                             "after": _blocks(new_findings)})  # fmt: skip
        if not kept:
            _discard(ctx, draft, new_draft, rule)
            continue
        _persist(ctx, draft, new_draft, rule)
        steps.append({"rule": rule, "before": str(quote.total), "after": str(new_quote.total)})
        quote, draft, findings = new_quote, new_draft, new_findings
    events.emit(ctx, "quote.checked", "rfq", session_id=quote.session_id,
                payload={"quote": quote.id, "stage": "sent", "blocking": check.blocking(findings),
                         "fallback": quote.fallback, "total": str(quote.total), "repairs": steps,
                         "findings": check.as_payload(findings)})  # fmt: skip
    return quote


def _blocks(findings: list[check.Finding]) -> list[str]:
    return sorted({f.rule for f in findings if f.level == check.BLOCK})


def _better(old: list[check.Finding], new: list[check.Finding]) -> bool:
    """Fewer rules block, and none blocks that did not before."""
    before, after = set(_blocks(old)), set(_blocks(new))
    return len(after) < len(before) and after <= before


def _time_left(by: datetime | None) -> float:
    return float("inf") if by is None else (by - timing.now()).total_seconds()


def _error(ctx: Context, quote: Quote, where: str, exc: Exception) -> None:
    events.emit(ctx, "error", "rfq", session_id=quote.session_id,
                payload={"where": f"repair:{where}", "error": f"{type(exc).__name__}: {exc}"})  # fmt: skip


def _persist(ctx: Context, old: Draft, new: Draft, rule: str) -> None:
    """What the fallback must inherit: the left-out options and a changed reading of the request."""
    for record in new.vetoed[len(old.vetoed) :]:
        events.emit(ctx, "option.vetoed", "rfq", session_id=new.rfq.session_id, payload={**record, "rule": rule})
    if new.rfq != old.rfq:
        events.emit(ctx, "rfq.extracted", "rfq", session_id=new.rfq.session_id, message_id=new.rfq.message_id,
                    payload=new.rfq.model_dump(mode="json"))  # fmt: skip


def request(ctx: Context, rfq: Rfq) -> tuple[str, tuple[str, ...]]:
    """The customer's mail as the check reads it (subject and body) and its file names."""
    row = ctx.db.execute("SELECT data FROM messages WHERE id = ?", (rfq.message_id,)).fetchone()
    message = Message.model_validate_json(row["data"]) if row else None
    return (extract._text(message), tuple(a.filename for a in message.attachments)) if message else ("", ())


def _discard(ctx: Context, old: Draft, rejected: Draft, rule: str) -> None:
    """A rejected reading was priced again, and those prices are in the log as `option.found`: they are left out,
    so the fallback never quotes the cargo the guard turned down."""
    before = calc.chargeable_kg(old.rfq.weight_kg, old.rfq.volume_cbm)
    after = calc.chargeable_kg(rejected.rfq.weight_kg, rejected.rfq.volume_cbm)
    if after != before:
        events.emit(ctx, "option.vetoed", "rfq", session_id=old.rfq.session_id,
                    payload={"kind": "chargeable", "kg": str(after), "rule": f"{rule}:rejected"})  # fmt: skip


async def _without(ctx: Context, draft: Draft, build: Build, records: list[dict]) -> Repaired:
    """The quote built again without the options `records` leave out; None when that leaves nothing or changes
    nothing (R1: an answer beats none)."""
    fresh = [r for r in {tuple(sorted(r.items())): r for r in records}.values() if r not in draft.vetoed]
    if not fresh:
        return None
    if any(r["kind"] == "trucker" for r in fresh):
        # Every door option carries the one balanced trucker (`engine.combine`): combine again without it.
        out = (*draft.vetoed, *fresh)
        found = [o for o in session.for_request(ctx, draft.rfq) if not any(session.vetoed(o, r) for r in out)]
        kept = engine.combine(draft.rfq, found, feedback.pickup_rule(ctx), ctx)
        kept = [o for o in kept if o.trucker] if draft.rfq.door_pickup else kept
    else:
        kept = [o for o in draft.options if not any(session.vetoed(o, r) for r in fresh)]
    if not kept or kept == draft.options:
        return None
    new = replace(draft, options=kept, vetoed=(*draft.vetoed, *fresh))
    return await build(new), new


# --- the repairs: each returns the rebuilt quote and its draft, or None when it has nothing to offer --------------


async def _distrusted(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`implausible_bid`: leave out the distrusted bid (round 67: a waiting charge per hour as the pickup)."""
    found = check.distrusted(quote, session.for_request(ctx, draft.rfq))
    return await _without(ctx, draft, tools.build, [session.veto(o, o.trucker is not None) for o in found])


async def _excluded(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`excluded_carrier`: leave out every option of an airline the customer ruled out (round 42)."""
    allowed = selection.allowed(draft.rfq, draft.options)
    return await _without(ctx, draft, tools.build, [{"kind": "airline", "name": o.carrier}
                                         for o in draft.options if o not in allowed])  # fmt: skip


async def _off_lane(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`lane`: leave out options of another lane (`session.for_request` keeps to the lane: nothing to record)."""
    r = draft.rfq
    kept = [o for o in draft.options if (o.origin, o.destination) == (r.origin, r.destination)]
    if not kept or len(kept) == len(draft.options):
        return None
    new = replace(draft, options=kept)
    return await tools.build(new), new


async def _language(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`language_sent`: the customer's language read again from their own words; our text when we have one, else
    the checked translation, which needs time left (rounds 70 and 72)."""
    language = extract._language(extract.own_words(request(ctx, draft.rfq)[0]))
    if language == compose.language_of(draft.rfq):
        return None
    if language not in compose.TEXT and _time_left(tools.by) < TRANSLATE_MIN_S:
        return None
    new = replace(draft, rfq=draft.rfq.model_copy(update={"language": language}))
    return await tools.build(new), new


async def _plain(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`text_*`: the mail rendered with the fixed phrases only, no model-written slot."""
    if draft.prose is None:
        return None
    new = replace(draft, prose=None)
    return await tools.build(new), new


async def _dg_note(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`dangerous_goods`: a translation may drop the papers the request needs; the note goes back in."""
    found = dangerous.declared(ctx, draft.rfq)
    if not found:
        return None
    goods, source = found
    body = dangerous.with_note(quote.body, goods, compose.language_of(draft.rfq), source)
    return quote.model_copy(update={"body": body}), draft


CRITIC_WAIT_S = 6.0  # gpt-4.1 answers in about 1.5 s
CRITIC_SYSTEM = (
    "An air freight customer's request. Code read the shipment's total gross {field} in more than one way. Pick from "
    "the list the one value that is the whole shipment's gross {field} as the customer states it, and copy the exact "
    "words of the request it comes from. null when no value in the list is."
)
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": ["number", "null"]}, "quote": {"type": "string"}},
    "required": ["value", "quote"],
    "additionalProperties": False,
}
FIELDS = {"weight": "weight_kg", "weight_unexplained": "weight_kg", "volume_unexplained": "volume_cbm"}


def _readings(field: str, text: str, pieces: int | None) -> set[Decimal]:
    return check.weight_readings(text) if field == "weight_kg" else check.volume_readings(text, pieces)


def _near(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.01"), abs(b) * Decimal("0.005"))


def _distinct(values: set[Decimal]) -> list[Decimal]:
    out: list[Decimal] = []
    for v in sorted(values):
        if v > 0 and not any(_near(v, o) for o in out):
            out.append(v)
    return out


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


async def critic(
    ctx: Context, rfq: Rfq, field: str, text: str, candidates: list[Decimal], by: datetime | None
) -> Decimal | None:
    """One small model call that picks among the readings code made, never a figure of its own. Its answer counts
    only when the words it quotes stand in the request, those words alone give that reading, and the reading is one
    of `candidates`: two independent readers agree. None on any doubt, failure or lack of time."""
    wait = min(CRITIC_WAIT_S, _time_left(by))
    if wait <= 1 or len(candidates) < 2:
        return None
    unit = "kg" if field == "weight_kg" else "cbm"
    listed = ", ".join(f"{c.normalize():f} {unit}" for c in candidates)
    try:
        result = await asyncio.wait_for(
            llm.ask_json(ctx, purpose="rfq.critic", system=CRITIC_SYSTEM.format(field=unit.replace("cbm", "volume")),
                         user=f"{text[:2500]}\n\nReadings: {listed}", schema=CRITIC_SCHEMA,
                         session_id=rfq.session_id, max_output_tokens=80, model=ctx.settings.model_check or None),
            wait,
        )  # fmt: skip
        value, said = result.data.get("value"), str(result.data.get("quote") or "")
        picked = Decimal(str(value)) if value is not None else None
    except Exception as exc:  # noqa: BLE001  no answer: the quote stays as it was
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "repair:critic", "error": type(exc).__name__})  # fmt: skip
        return None
    grounded = (
        picked is not None
        and len(said.strip()) >= 3
        and _flat(said) in _flat(text)
        and any(_near(picked, c) for c in candidates)
        and any(_near(picked, r) for r in _readings(field, said, rfq.pieces))
    )
    events.emit(ctx, "rfq.critic", "rfq", session_id=rfq.session_id,
                payload={"field": field, "candidates": [str(c) for c in candidates], "value": str(picked),
                         "quote": said[:200], "taken": grounded})  # fmt: skip
    if not grounded:
        return None
    return next(c for c in candidates if _near(picked, c))


async def _reading(ctx: Context, quote: Quote, draft: Draft, tools: Tools) -> Repaired:
    """`weight`, `weight_unexplained`, `volume_unexplained`: the figure the customer's own words give, when code reads
    exactly one (the lots added up, a single reading), else the one `critic` picks from code's readings; the request
    is priced again at it (sheets and the portal; airline prices for the old chargeable weight are left out, the
    pickup stays) and the quote built again. Nothing without a way to price again."""
    if tools.reprice is None:
        return None
    rule = tools.rule
    field, r = FIELDS[rule], draft.rfq
    text = extract.own_words(request(ctx, r)[0])
    if rule == "weight" and (lots := check.lot_weight(text)) is not None:
        value = lots
    else:
        candidates = _distinct(_readings(field, text, r.pieces))
        if len(candidates) == 1:
            value = candidates[0]
        else:
            value = await critic(ctx, r, field, text, candidates, tools.by)
    if value is None or _near(value, getattr(r, field)):
        return None
    new_rfq = r.model_copy(update={field: value})
    options = await tools.reprice(new_rfq)
    if not options:
        return None
    old_kg, new_kg = (
        calc.chargeable_kg(r.weight_kg, r.volume_cbm),
        calc.chargeable_kg(new_rfq.weight_kg, new_rfq.volume_cbm),
    )
    stale = ({"kind": "chargeable", "kg": str(old_kg)},) if old_kg != new_kg else ()
    new = replace(draft, rfq=new_rfq, options=options, vetoed=(*draft.vetoed, *stale))
    return await tools.build(new), new


REPAIRS: dict[str, Callable[..., Awaitable[Repaired]]] = {
    "implausible_bid": _distrusted,
    "excluded_carrier": _excluded,
    "lane": _off_lane,
    "language_sent": _language,
    "text_total": _plain,
    "text_amounts": _plain,
    "text_leftover": _plain,
    "dangerous_goods": _dg_note,
    "weight": _reading,
    "weight_unexplained": _reading,
    "volume_unexplained": _reading,
}
