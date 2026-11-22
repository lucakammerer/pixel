"""The last reader before a quote leaves (R33). The rounds only one to three teams won were each the first of a new
kind of requirement, and `check.py` only knows the kinds we already lost. One gateway call reads the customer's
request beside the exact text about to be sent and lists every ask the reply does not meet, each with the customer's
own sentence and a class. `wording` gains the sentence that meets it, kept only when the sent-text check blocks on
nothing new. `price`, `selection` and `scope` are never guessed: the finding goes to `PIXEL_ALERT_URL` at once and
this one quote waits until `target_at` for a person in the session UI, then leaves as it is. The model never prices
or selects. `PIXEL_UNMET`: `on`, `log` (read and record, change nothing) or `off`."""

import asyncio
from collections.abc import Callable
from datetime import datetime

from pixel import llm
from pixel.agents.rfq import check, compose
from pixel.contracts import Quote
from pixel.core import events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes

WAIT_S = 8  # gpt-4.1 answers in about 2 s
HOLD_POLL_S = 0.5
MAX_CHARS = 6000  # of the request: a long packing list is cut
MAX_ADD = 300
HELD = ("price", "selection", "scope")
CLASSES = ("wording", *HELD)
PURPOSE = "quote.unmet"
SYSTEM = (
    "You are the last reader before an air freight forwarder's quote leaves. You get the customer's request (the "
    "mail and what its attached documents hold) and the reply exactly as it will be sent, with the files it "
    "carries. List every ask, condition or requirement of the customer that the reply does not satisfy; also a "
    "reply not written in the language the customer writes in, and a document of the request that states what the "
    "reply ignores although the shipment depends on it. For each give `sentence`: the customer's own sentence that "
    "sets it, copied exactly from the request. `class`: wording (something to state, confirm, name, address or "
    "format in the reply, with no effect on the amount or the chosen option), price (something that changes the "
    "amount: a service, a fee, an insurance, a currency), selection (something that changes which option may be "
    "chosen: an airline, a routing, a date, a transit limit) or scope (what is quoted: several shipments, another "
    "lane, alternatives). `add`: for wording only, the sentence for the reply that meets the ask, in the customer's "
    "language and the reply's voice; an empty string for every other class and when meeting the ask needs an "
    "amount, a figure, a date or a fact that neither the request nor the reply holds. Do not judge whether the "
    "departure airport, the airline, the trucker or the amounts are well chosen or correct, code checked that: "
    "list only what the customer asks for and the reply does not do. Never write, compute or compare a price. A "
    "fact the customer only states is no ask. Never list what the reply already does. An empty list when nothing "
    "is unmet."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "unmet": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence": {"type": "string"},
                    "class": {"type": "string", "enum": list(CLASSES)},
                    "add": {"type": "string"},
                },
                "required": ["sentence", "class", "add"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["unmet"],
    "additionalProperties": False,
}
Inspect = Callable[[Quote], list[check.Finding]]


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


async def read(
    ctx: Context, request: str, subject: str, body: str, files: tuple[str, ...], wait: float, session_id: int | None
) -> list[dict]:
    """What the model lists, each as `class`, `sentence` and `add`; only asks whose sentence stands in the request,
    so a requirement the model made up moves nothing. Raises on a failed, late or malformed answer."""
    request = request[:MAX_CHARS]
    carried = f"it carries {', '.join(files)}: this offer with its price" if files else "it carries no file"
    user = f"REQUEST\n{request}\n\nREPLY ({carried})\nSubject: {subject}\n\n{body}"
    result = await asyncio.wait_for(
        llm.ask_json(ctx, purpose=PURPOSE, system=SYSTEM, user=user, schema=SCHEMA, session_id=session_id,
                     max_output_tokens=300, model=ctx.settings.model_check or None),
        wait,
    )  # fmt: skip
    said = _flat(request)
    return [
        {"class": a["class"], "sentence": a["sentence"].strip(), "add": a["add"].strip()}
        for a in result.data["unmet"]
        if a["class"] in CLASSES and len(a["sentence"].strip()) >= 3 and _flat(a["sentence"]) in said
    ]


def _addable(ask: dict, request: str, body: str) -> bool:
    """A sentence code may add: a wording ask's, short, and with no figure the request and the reply do not hold."""
    add = ask["add"]
    known = compose._figures(request) | compose._figures(body)
    return ask["class"] == "wording" and 0 < len(add) <= MAX_ADD and compose._figures(add) <= known


def _with(body: str, lines: list[str]) -> str:
    """The sentences before the paragraph above the signature, as `dangerous.with_note`."""
    paragraphs = body.split("\n\n")
    signed = next((i for i, p in enumerate(paragraphs) if compose.COMPANY in p), len(paragraphs) - 1)
    at = max(signed - 1, 1)
    return "\n\n".join([*paragraphs[:at], " ".join(lines), *paragraphs[at:]])


def _mended(quote: Quote, asks: list[dict], request: str, inspect: Inspect) -> Quote:
    """`quote` with the sentences that meet its wording asks, or `quote` itself when the check on the new text
    blocks on a rule the old text passed (an amount, the language, the PDF)."""
    lines = [a["add"] for a in asks if _addable(a, request, quote.body)]
    if not lines:
        return quote
    new = quote.model_copy(update={"body": _with(quote.body, lines)})
    blocks = {f.rule for f in inspect(quote) if f.level == check.BLOCK}
    return new if {f.rule for f in inspect(new) if f.level == check.BLOCK} <= blocks else quote


async def _hold(ctx: Context, session_id: int, until: datetime) -> None:
    """Until `until`, or until a person took the session over or quoted it; `outbox` then decides what leaves."""
    while (left := (until - timing.now()).total_seconds()) > 0:
        if outbox.taken_over(ctx, session_id) or outbox.quoted(ctx, session_id):
            return
        await asyncio.sleep(min(HOLD_POLL_S, left))


async def settle(ctx: Context, quote: Quote, request: str, deadline: Deadline, inspect: Inspect) -> Quote:
    """The quote to send: `quote`, or `quote` with its wording asks met. Emits `quote.unmet`; an ask left open
    alerts, one of `HELD` also holds the quote until `deadline.target_at`. One call per session, at most `WAIT_S`;
    never raises."""
    mode = ctx.settings.unmet
    wait = min(WAIT_S, (deadline.hard_at - timing.now()).total_seconds())
    if mode == "off" or wait <= 0 or _asked(ctx, quote.session_id):
        return quote
    try:
        asks = await read(ctx, request, quote.subject, quote.body, (f"offer-{quote.id}.pdf",), wait, quote.session_id)
        sent = _mended(quote, asks, request, inspect) if mode == "on" else quote
    except Exception as exc:  # noqa: BLE001  unread: the quote goes out as it is
        events.emit(ctx, "error", "rfq", session_id=quote.session_id,
                    payload={"where": "unmet", "error": type(exc).__name__})  # fmt: skip
        return quote
    for ask in asks:
        ask["met"] = sent is not quote and _addable(ask, request, quote.body)
    left = sorted({a["class"] for a in asks if not a["met"]})
    held = mode == "on" and any(c in HELD for c in left) and timing.now() < deadline.target_at
    events.emit(ctx, "quote.unmet", "rfq", session_id=quote.session_id,
                payload={"quote": quote.id, "mode": mode, "asks": asks, "open": left,
                         "hold_until": deadline.target_at.isoformat() if held else None})  # fmt: skip
    if mode != "on":
        return quote
    if sent is not quote:
        quotes.save(ctx, sent)
    if left and ctx.settings.alert_url:
        poller.spawn(ctx, feedback.post(ctx, [{"issue": "unmet_ask", "session": quote.session_id,
                                               "at": timing.now().isoformat()[:19], "classes": left,
                                               "held": held}]))  # fmt: skip
    if held:
        await _hold(ctx, quote.session_id, deadline.target_at)
    return sent


def _asked(ctx: Context, session_id: int) -> bool:
    return bool(
        ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND (kind = 'quote.unmet' OR kind = 'llm.call'"
            " AND json_extract(payload, '$.purpose') = ?) LIMIT 1",
            (session_id, PURPOSE),
        ).fetchone()
    )
