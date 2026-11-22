"""Voice calls through ElevenLabs: a person on the session page calls a carrier whose bid is late or a customer who
has not yet answered our question for a missing fact. The browser holds the call (`call.js`); the agent's client tool
sends what it heard here, and `answer` turns it into an incoming mail in the thread, so the router and the same rules
read it as if it had been mailed: a carrier's price by `pricing/bids.py` `record`, a customer's fact by
`agents/rfq/extract.py` `_ask`. The agent only asks and repeats back; it never names, compares or computes a price.
Every step is an event (`call.*`); nothing about a call is kept in memory."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from pixel.api.mailbox import RATEDESK
from pixel.contracts import Event, Message
from pixel.core import events, timing
from pixel.core.context import Context

EL = "https://api.elevenlabs.io/v1"
# A live call holds the wait for a bid or an answer open this long from its start, also when the browser never
# reports its end. Calls start only while the request can still be priced after it (`START_MARGIN`).
CALL_MAX_S = 120
END_GRACE_S = 5  # after the call ended: its answer is stored, the bid is still being read
START_MARGIN = timedelta(seconds=30)  # left for pricing and sending between a call's longest end and the hard deadline
# Ids of call answers: far above every mailbox id, so `_ask`'s "after our question" still holds, and below 2**53 so
# the browser keeps them exact.
VOICE_IDS = 4 * 10**15
FACT_NAMES = {"pickup": "the pickup postcode", "origin": "the departure airport", "destination": "the destination airport",
              "weight": "the gross weight in kg", "volume": "the volume in cubic metres or the dimensions"}  # fmt: skip
IATA = re.compile(r"^[A-Z]{3}$")
POSTCODE = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,9}$")


class CallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    """Someone a person may call for this session, and the mail of ours the call follows up."""

    party: str  # "carrier" or "customer"
    name: str
    email: str
    asked_id: int  # our bid request or our question
    pickup: bool = False  # a trucker's pickup bid
    missing: tuple[str, ...] = ()  # the customer's facts, keys of `extract.FACTS`


def configured(ctx: Context) -> bool:
    return bool(ctx.settings.elevenlabs_api_key and ctx.settings.elevenlabs_agent_id)


def _received_at(ctx: Context, session_id: int) -> datetime | None:
    row = ctx.db.execute("SELECT sent_at FROM messages WHERE id = ?", (session_id,)).fetchone()
    return datetime.fromisoformat(row["sent_at"]) if row else None


def _last_start(ctx: Context, session_id: int) -> datetime | None:
    """The latest moment a call may start: its longest hold still ends `START_MARGIN` before the hard deadline."""
    from pixel.core.orchestrator import deadline_for

    received = _received_at(ctx, session_id)
    if received is None:
        return None
    return deadline_for(received, ctx.settings).hard_at - START_MARGIN - timedelta(seconds=CALL_MAX_S)


def targets(ctx: Context, session_id: int) -> list[Target]:
    """Carriers asked for a bid that has not come while the bids are still collected, and the customer while
    `_ask` still waits for the answer to our question. None once the quote is out (R4: only the first counts) or too
    late for a call to change it: a later answer would be read by nothing."""
    from pixel.agents.rfq.extract import ASK_WAIT_S, FACTS
    from pixel.core import outbox
    from pixel.core.orchestrator import deadline_for

    last, received = _last_start(ctx, session_id), _received_at(ctx, session_id)
    now = timing.now()
    if last is None or received is None or now > last or outbox.quoted(ctx, session_id):
        return []
    deadline = deadline_for(received, ctx.settings)
    found: list[Target] = []
    evs = events.for_session(ctx, session_id)
    answered = {e.payload.get("request_id") for e in evs if e.kind == "bid.received"}
    for e in evs:
        p = e.payload
        # `bids.collect` waits until the target or the bid wait after our ask, whichever is later.
        collecting = now < max(deadline.target_at, e.ts + timedelta(seconds=ctx.settings.bid_wait_s))
        if (e.kind == "bid.requested" and collecting and e.message_id not in answered
                and p.get("email", "").lower() != RATEDESK):  # fmt: skip
            found.append(
                Target("carrier", p.get("carrier") or p["email"], p["email"], e.message_id, bool(p.get("pickup")))
            )
    question = ctx.db.execute(
        "SELECT id, recipient, body FROM messages WHERE session_id = ? AND direction = 'out' AND purpose = 'question'"
        " ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    waiting = now < received + timedelta(seconds=ASK_WAIT_S) or holding(ctx, session_id, "customer")
    if (
        question
        and waiting
        and not ctx.db.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND direction = 'in' AND id > ? AND lower(sender) = ?",
            (session_id, question["id"], question["recipient"].lower()),
        ).fetchone()
    ):
        missing = tuple(fact for fact, words in FACTS.items() if any(w in question["body"] for w in words))
        found.append(Target("customer", question["recipient"], question["recipient"], question["id"], missing=missing))
    return found


def _started(ctx: Context, call_id: int) -> Event:
    row = ctx.db.execute("SELECT session_id FROM events WHERE id = ? AND kind = 'call.started'", (call_id,)).fetchone()
    if row is None:
        raise CallError(404, "Unknown call.")
    return next(e for e in events.for_session(ctx, row["session_id"], call_id - 1) if e.id == call_id)


def _calls(ctx: Context, session_id: int) -> dict[int, list[Event]]:
    """Every call of the session by its id, with its later events."""
    calls: dict[int, list[Event]] = {}
    for e in events.for_session(ctx, session_id):
        if e.kind == "call.started":
            calls[e.id] = [e]
        elif e.kind.startswith("call.") and e.payload.get("call_id") in calls:
            calls[e.payload["call_id"]].append(e)
    return calls


def holding(ctx: Context, session_id: int, party: str) -> bool:
    """True while a call to this party of the session is live, or just ended: the wait for its answer stays open."""
    now = timing.now()
    recent = (now - timedelta(seconds=CALL_MAX_S)).isoformat()
    # Read every poll of `_ask` and `collect`: without a recent call the session's log is not read at all.
    if not ctx.db.execute("SELECT 1 FROM events WHERE session_id = ? AND kind = 'call.started' AND ts > ? LIMIT 1",
                          (session_id, recent)).fetchone():  # fmt: skip
        return False
    for started, *later in _calls(ctx, session_id).values():
        if started.payload.get("party") != party or now >= started.ts + timedelta(seconds=CALL_MAX_S):
            continue
        ended = next((e for e in later if e.kind == "call.ended"), None)
        if ended is None or now < ended.ts + timedelta(seconds=END_GRACE_S):
            return True
    return False


async def _el(ctx: Context, path: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{EL}{path}", headers={"xi-api-key": ctx.settings.elevenlabs_api_key})
    if response.status_code != 200:
        raise CallError(502, f"ElevenLabs answered {response.status_code}.")
    return response.json()


def _shipment(ctx: Context, session_id: int) -> tuple[str, str]:
    from pixel.pricing import session

    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return "", ""
    pieces = f", {rfq.pieces} pieces" if rfq.pieces else ""
    return f"{rfq.origin} to {rfq.destination}", f"{rfq.weight_kg} kg, {rfq.volume_cbm} cubic metres{pieces}"


def _wanted(target: Target) -> str:
    if target.party == "customer":
        names = [FACT_NAMES[f] for f in target.missing] or ["the details we still need"]
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    if target.pickup:
        return "your all-in price for the pickup with its currency, and how many hours until you can collect"
    return "your all-in price with its currency, including freight, fuel and security, and the transit time in days"


async def start(ctx: Context, session_id: int, email: str) -> dict:
    """A signed URL for one browser call to `email`, and what the agent says. `call.started` is its record."""
    if not configured(ctx):
        raise CallError(503, "Voice calls are not set up: ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID.")
    target = next((t for t in targets(ctx, session_id) if t.email.lower() == email.lower()), None)
    if target is None:
        raise CallError(409, "Nobody to call: the bid or the answer arrived, the quote is out or it is too late.")
    if holding(ctx, session_id, target.party):
        raise CallError(409, "A call for this session is already running.")
    signed = await _el(ctx, f"/convai/conversation/get-signed-url?agent_id={ctx.settings.elevenlabs_agent_id}")
    lane, shipment = _shipment(ctx, session_id)
    variables = {
        "party": target.party,
        "name": target.name,
        "reference": str(session_id),
        "lane": lane,
        "shipment": shipment,
        "wanted": _wanted(target),
        "signer": ctx.settings.signer,
    }
    started = events.emit(ctx, "call.started", "voice", session_id=session_id, message_id=target.asked_id,
                          payload=asdict(target) | {"variables": variables})  # fmt: skip
    return {"call_id": started.id, "signed_url": signed["signed_url"], "dynamic_variables": variables}


def connected(ctx: Context, call_id: int, conversation_id: str) -> None:
    started = _started(ctx, call_id)
    if not re.fullmatch(r"[\w-]{1,100}", conversation_id):
        raise CallError(400, "Invalid conversation id.")
    events.emit(ctx, "call.connected", "voice", session_id=started.session_id,
                payload={"call_id": call_id, "conversation_id": conversation_id})  # fmt: skip


def ended(ctx: Context, call_id: int, error: str = "") -> None:
    started = _started(ctx, call_id)
    if any(e.kind == "call.ended" for e in _calls(ctx, started.session_id).get(call_id, [])):
        return
    events.emit(ctx, "call.ended", "voice", session_id=started.session_id,
                payload={"call_id": call_id} | ({"error": error[:300]} if error else {}))  # fmt: skip


def _decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise CallError(400, f"{name} is not a number.") from None
    if not number.is_finite() or number <= 0:
        raise CallError(400, f"{name} must be above zero.")
    return number


def body(target: Target, fields: dict) -> str:
    """What the caller said, in the words our rules read in a mailed answer: a carrier's offer as the airlines and
    truckers write it, a customer's fact as a plain sentence. Only the fields the call is for count."""
    said = "Given by phone to PIXEL Air Freight.\n\n"
    if target.party == "carrier":
        total = _decimal(fields.get("total"), "total")
        currency = str(fields.get("currency", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CallError(400, "currency must be a three-letter code.")
        if target.pickup:
            hours = fields.get("pickup_hours")
            hours = (
                f"\n\nPickup within {int(_decimal(hours, 'pickup_hours'))} hours." if hours not in (None, "") else ""
            )
            return f"{said}Our all-in rate for the pickup is {total} {currency}.{hours}"
        days = int(_decimal(fields.get("transit_days"), "transit_days"))
        return (f"{said}Our all-in rate is {total} {currency}, inclusive of freight, fuel and security charges.\n\n"
                f"The transit time is {days} days.")  # fmt: skip
    lines = []
    for fact in target.missing:
        if fact in ("origin", "destination") and (code := str(fields.get(fact) or "").strip().upper()):
            if not IATA.fullmatch(code):
                raise CallError(400, f"{fact} must be a three-letter airport code.")
            lines.append(f"The goods leave from {code}." if fact == "origin" else f"The destination airport is {code}.")
        elif fact == "weight" and fields.get("weight_kg") not in (None, ""):
            lines.append(f"The gross weight is {_decimal(fields['weight_kg'], 'weight_kg')} kg.")
        elif fact == "volume" and fields.get("volume_cbm") not in (None, ""):
            lines.append(f"The volume is {_decimal(fields['volume_cbm'], 'volume_cbm')} cbm.")
        elif fact == "pickup" and (code := str(fields.get("pickup_postcode") or "").strip().upper()):
            if not POSTCODE.fullmatch(code):
                raise CallError(400, "pickup_postcode is not a postcode.")
            lines.append(f"Pickup postcode: {code}.")
    if not lines:
        raise CallError(400, "Nothing the request is missing was given.")
    return said + "\n".join(lines)


def answer(ctx: Context, call_id: int, fields: dict) -> int:
    """The agent's client tool during the call: stores what it heard as an incoming mail in the thread and hands it
    to the orchestrator like a polled one. One answer per call; returns the mail's id."""
    from pixel.core import orchestrator, poller

    started = _started(ctx, call_id)
    session_id = started.session_id
    calls = _calls(ctx, session_id).get(call_id, [])
    if any(e.kind == "call.answer" for e in calls):
        raise CallError(409, "This call's answer is already recorded.")
    if timing.now() >= started.ts + timedelta(seconds=CALL_MAX_S) or any(e.kind == "call.ended" for e in calls):
        raise CallError(409, "This call is over.")
    target = Target(**{k: tuple(v) if k == "missing" else v for k, v in started.payload.items() if k != "variables"})
    text = body(target, fields)
    asked = ctx.db.execute("SELECT subject, sender FROM messages WHERE id = ?", (target.asked_id,)).fetchone()
    subject = asked["subject"] if asked else f"Session {session_id}"
    now = timing.now()
    raw = {"id": VOICE_IDS + call_id, "from": target.email, "to": asked["sender"] if asked else "",
           "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}", "body": text,
           "in_reply_to": target.asked_id, "references": [target.asked_id], "received_at": now.isoformat(),
           "attachments": [], "channel": "voice", "call_id": call_id}  # fmt: skip
    message = Message(id=raw["id"], session_id=session_id, direction="in", sender=raw["from"], to=raw["to"],
                      subject=raw["subject"], body=text, in_reply_to=target.asked_id, references=(target.asked_id,),
                      sent_at=now, raw=raw)  # fmt: skip
    if not poller.accept(ctx, message, lambda m: orchestrator.dispatch(ctx, m)):
        raise CallError(500, "The answer could not be stored.")
    events.emit(ctx, "call.answer", "voice", session_id=session_id, message_id=message.id,
                payload={"call_id": call_id, "fields": {k: str(v) for k, v in fields.items()}})  # fmt: skip
    return message.id


async def transcript(ctx: Context, call_id: int) -> dict | None:
    """ElevenLabs' summary and transcript of a finished call, fetched until it is done and then kept as
    `call.transcript`. None while ElevenLabs is still processing it."""
    started = _started(ctx, call_id)
    calls = _calls(ctx, started.session_id).get(call_id, [])
    if kept := next((e for e in calls if e.kind == "call.transcript"), None):
        return kept.payload
    conversation = next((e.payload["conversation_id"] for e in calls if e.kind == "call.connected"), None)
    if conversation is None:
        return None
    call = await _el(ctx, f"/convai/conversations/{conversation}")
    if call.get("status") not in ("done", "failed"):
        return None
    payload = {
        "call_id": call_id,
        "status": call.get("status"),
        "summary": (call.get("analysis") or {}).get("transcript_summary"),
        "duration_s": (call.get("metadata") or {}).get("call_duration_secs"),
        "transcript": [
            {"role": t.get("role"), "message": t["message"]} for t in call.get("transcript") or [] if t.get("message")
        ],
    }
    events.emit(ctx, "call.transcript", "voice", session_id=started.session_id, payload=payload)
    return payload
