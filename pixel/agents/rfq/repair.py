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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime

from pixel.agents.rfq import check, compose, dangerous, extract
from pixel.contracts import Message, Quote, RateOption, Rfq
from pixel.core import events, feedback, timing
from pixel.core.context import Context
from pixel.pricing import engine, selection, session

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
Repaired = tuple[Quote, Draft] | None


async def settle(
    ctx: Context, quote: Quote, draft: Draft, build: Build, inspect: Inspect, *, by: datetime | None
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
            candidate = await REPAIRS[rule](ctx, quote, draft, by, build)
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


async def _distrusted(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`implausible_bid`: leave out the distrusted bid (round 67: a waiting charge per hour as the pickup)."""
    found = check.distrusted(quote, session.for_request(ctx, draft.rfq))
    return await _without(ctx, draft, build, [session.veto(o, o.trucker is not None) for o in found])


async def _excluded(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`excluded_carrier`: leave out every option of an airline the customer ruled out (round 42)."""
    allowed = selection.allowed(draft.rfq, draft.options)
    return await _without(ctx, draft, build, [{"kind": "airline", "name": o.carrier}
                                         for o in draft.options if o not in allowed])  # fmt: skip


async def _off_lane(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`lane`: leave out options of another lane (`session.for_request` keeps to the lane: nothing to record)."""
    r = draft.rfq
    kept = [o for o in draft.options if (o.origin, o.destination) == (r.origin, r.destination)]
    if not kept or len(kept) == len(draft.options):
        return None
    new = replace(draft, options=kept)
    return await build(new), new


async def _language(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`language_sent`: the customer's language read again from their own words; our text when we have one, else
    the checked translation, which needs time left (rounds 70 and 72)."""
    language = extract._language(extract.own_words(request(ctx, draft.rfq)[0]))
    if language == compose.language_of(draft.rfq):
        return None
    if language not in compose.TEXT and _time_left(by) < TRANSLATE_MIN_S:
        return None
    new = replace(draft, rfq=draft.rfq.model_copy(update={"language": language}))
    return await build(new), new


async def _plain(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`text_*`: the mail rendered with the fixed phrases only, no model-written slot."""
    if draft.prose is None:
        return None
    new = replace(draft, prose=None)
    return await build(new), new


async def _dg_note(ctx: Context, quote: Quote, draft: Draft, by: datetime | None, build: Build) -> Repaired:
    """`dangerous_goods`: a translation may drop the papers the request needs; the note goes back in."""
    found = dangerous.declared(ctx, draft.rfq)
    if not found:
        return None
    goods, source = found
    body = dangerous.with_note(quote.body, goods, compose.language_of(draft.rfq), source)
    return quote.model_copy(update={"body": body}), draft


REPAIRS: dict[str, Callable[..., Awaitable[Repaired]]] = {
    "implausible_bid": _distrusted,
    "excluded_carrier": _excluded,
    "lane": _off_lane,
    "language_sent": _language,
    "text_total": _plain,
    "text_amounts": _plain,
    "text_leftover": _plain,
    "dangerous_goods": _dg_note,
}
