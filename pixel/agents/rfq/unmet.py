"""The last reader before a quote leaves (R33). The rounds only one to three teams won were each the first of a new
kind of requirement, and `check.py` only knows the kinds we already lost. One gateway call reads the customer's
request beside the exact text about to be sent and lists every ask the reply does not meet, each with the customer's
own sentence and a class. It never changes the reply's text. In `on` every open ask goes to `PIXEL_ALERT_URL` at
once, and one of `price`, `selection` or `scope` also holds this one quote until `target_at` for a person in the
session UI, then it leaves as it is. `PIXEL_UNMET`: `off` (the default: no call), `log` (read and record only) or
`on`."""

import asyncio
from datetime import datetime

from pixel import llm
from pixel.contracts import Quote
from pixel.core import events, feedback, outbox, poller, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline

WAIT_S = 8  # gpt-4.1 answers in about 2 s
HOLD_POLL_S = 0.5
MAX_CHARS = 6000  # of the request: a long packing list is cut
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
    "lane, alternatives). Do not judge whether the departure airport, the airline, the trucker or the amounts are "
    "well chosen or correct, code checked that: list only what the customer asks for and the reply does not do. "
    "Never write, compute or compare a price. A fact the customer only states is no ask. Never list what the reply "
    "already does. An empty list when nothing is unmet."
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
                },
                "required": ["sentence", "class"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["unmet"],
    "additionalProperties": False,
}


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


async def read(
    ctx: Context, request: str, subject: str, body: str, files: tuple[str, ...], wait: float, session_id: int | None
) -> list[dict]:
    """What the model lists, each as `class` and `sentence`; only asks whose sentence stands in the request,
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
        {"class": a["class"], "sentence": a["sentence"].strip()}
        for a in result.data["unmet"]
        if a["class"] in CLASSES and len(a["sentence"].strip()) >= 3 and _flat(a["sentence"]) in said
    ]


async def _hold(ctx: Context, session_id: int, until: datetime) -> None:
    """Until `until`, or until a person took the session over or quoted it; `outbox` then decides what leaves."""
    while (left := (until - timing.now()).total_seconds()) > 0:
        if outbox.taken_over(ctx, session_id) or outbox.quoted(ctx, session_id):
            return
        await asyncio.sleep(min(HOLD_POLL_S, left))


async def settle(ctx: Context, quote: Quote, request: str, deadline: Deadline, by: datetime) -> None:
    """Emits `quote.unmet`; in `on` an open ask alerts, one of `HELD` also holds the quote until
    `deadline.target_at`. Reading and holding end by `by`, before the orchestrator takes the request over. One call
    per session, at most `WAIT_S`; never raises."""
    mode = ctx.settings.unmet
    if mode == "off":
        return
    until = min(deadline.target_at, by)
    wait = min(WAIT_S, (by - timing.now()).total_seconds())
    try:
        if wait <= 0 or _asked(ctx, quote.session_id):
            return
        asks = await read(ctx, request, quote.subject, quote.body, (f"offer-{quote.id}.pdf",), wait, quote.session_id)
        left = sorted({a["class"] for a in asks})
        held = mode == "on" and any(c in HELD for c in left) and timing.now() < until
        events.emit(ctx, "quote.unmet", "rfq", session_id=quote.session_id,
                    payload={"quote": quote.id, "mode": mode, "asks": asks, "open": left,
                             "hold_until": until.isoformat() if held else None})  # fmt: skip
        if mode == "on" and left and ctx.settings.alert_url:
            poller.spawn(ctx, feedback.post(ctx, [{"issue": "unmet_ask", "session": quote.session_id,
                                                   "at": timing.now().isoformat()[:19], "classes": left,
                                                   "held": held}]))  # fmt: skip
        if held:
            await _hold(ctx, quote.session_id, until)
    except Exception as exc:  # noqa: BLE001  the quote goes out as it is
        events.emit(ctx, "error", "rfq", session_id=quote.session_id,
                    payload={"where": "unmet", "error": type(exc).__name__})  # fmt: skip


def _asked(ctx: Context, session_id: int) -> bool:
    return bool(
        ctx.db.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND (kind = 'quote.unmet' OR kind = 'llm.call'"
            " AND json_extract(payload, '$.purpose') = ?) LIMIT 1",
            (session_id, PURPOSE),
        ).fetchone()
    )
