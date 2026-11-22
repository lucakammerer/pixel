"""Voice calls through ElevenLabs:
- Order status demo calls: a person clicks a button linked to a specific shipment/order on the laptop, a simulated
  phone screen (`/phone` or inline) rings so it can be picked up, the ElevenLabs agent asks targeted questions about
  that order's status/location/ETA, and `answer_order` updates the shipment in the CRM and streams visual feedback
  live to the laptop screen.
- Late carrier / missing customer fact calls: a person on the session page calls a carrier whose bid is late or a
  customer who has not yet answered our question for a missing fact, injecting the answer as an incoming mail.

Every step is an event (`call.*`, `shipment.updated`); nothing about a call is kept in memory."""

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
RING_MAX_S = 120
END_GRACE_S = 5  # after the call ended: its answer is stored, the bid is still being read
START_MARGIN = timedelta(seconds=30)  # left for pricing and sending between a call's longest end and the hard deadline
# Ids of call answers: far above every mailbox id, so `_ask`'s "after our question" still holds, and below 2**53 so
# the browser keeps them exact.
VOICE_IDS = 4 * 10**15
FACT_NAMES = {"pickup": "the pickup postcode", "origin": "the departure airport", "destination": "the destination airport",
              "weight": "the gross weight in kg", "volume": "the volume in cubic metres or the dimensions"}  # fmt: skip
IATA = re.compile(r"^[A-Z]{3}$")
POSTCODE = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,9}$")

ORDER_STATUS_LABELS: dict[str, str] = {
    "booking": "Booking",
    "booked": "Booked",
    "confirmed": "Confirmed",
    "picked_up": "Picked up",
    "at_origin": "At origin hub",
    "in_transit": "In transit",
    "customs": "In customs",
    "arrived": "Arrived",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "delayed": "Delayed",
    "exception": "Exception",
}
ORDER_STATUS_ALIASES: dict[str, str] = {
    "booking": "booking",
    "booked": "booked",
    "confirmed": "confirmed",
    "picked up": "picked_up",
    "picked_up": "picked_up",
    "collected": "picked_up",
    "pickup complete": "picked_up",
    "at origin": "at_origin",
    "at_origin": "at_origin",
    "origin warehouse": "at_origin",
    "at airport": "at_origin",
    "at origin airport": "at_origin",
    "in transit": "in_transit",
    "in_transit": "in_transit",
    "transit": "in_transit",
    "airborne": "in_transit",
    "departed": "in_transit",
    "en route": "in_transit",
    "on the way": "in_transit",
    "customs": "customs",
    "in customs": "customs",
    "customs clearance": "customs",
    "cleared customs": "customs",
    "arrived": "arrived",
    "landed": "arrived",
    "at destination": "arrived",
    "out for delivery": "out_for_delivery",
    "out_for_delivery": "out_for_delivery",
    "delivered": "delivered",
    "completed": "delivered",
    "delayed": "delayed",
    "delay": "delayed",
    "held": "delayed",
    "on hold": "delayed",
    "exception": "exception",
    "issue": "exception",
    "problem": "exception",
    "damaged": "exception",
}


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


def normalize_order_status(raw: str) -> str:
    """Maps a spoken or model-supplied order status into a canonical status key."""
    cleaned = re.sub(r"[^a-z0-9_ ]+", " ", str(raw or "").strip().lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise CallError(400, "status is required.")
    if cleaned in ORDER_STATUS_ALIASES:
        return ORDER_STATUS_ALIASES[cleaned]
    underscored = cleaned.replace(" ", "_")
    if underscored in ORDER_STATUS_LABELS:
        return underscored
    for phrase, key in ORDER_STATUS_ALIASES.items():
        if phrase in cleaned:
            return key
    return underscored[:40]


def status_label(key: str) -> str:
    return ORDER_STATUS_LABELS.get(key, key.replace("_", " ").capitalize())


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
    from pixel.agents.rfq.extract import ASK_WAIT_S, FACTS, _unknown
    from pixel.core import outbox
    from pixel.core.orchestrator import deadline_for
    from pixel.pricing import session

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
        if not missing and (rfq := session.rfq(ctx, session_id)):
            missing = tuple(_unknown(rfq))
        found.append(Target("customer", question["recipient"], question["recipient"], question["id"], missing=missing))
    return found


def _call_root(ctx: Context, call_id: int) -> Event:
    row = ctx.db.execute(
        "SELECT session_id FROM events WHERE id = ? AND kind IN ('call.started', 'call.ringing')",
        (call_id,),
    ).fetchone()
    if row is None:
        raise CallError(404, "Unknown call.")
    return next(e for e in events.for_session(ctx, row["session_id"], call_id - 1) if e.id == call_id)


def _calls(ctx: Context, session_id: int) -> dict[int, list[Event]]:
    """Every call of the session by its root id (`call.started` or `call.ringing`), with its later events."""
    found: dict[int, list[Event]] = {}
    for e in events.for_session(ctx, session_id):
        if e.kind == "call.ringing":
            found[e.id] = [e]
        elif e.kind == "call.started":
            root_id = e.payload.get("call_id")
            if root_id in found:
                found[root_id].append(e)
            else:
                found[e.id] = [e]
        elif e.kind.startswith("call.") and e.payload.get("call_id") in found:
            found[e.payload["call_id"]].append(e)
    return found


def holding(ctx: Context, session_id: int, party: str) -> bool:
    """True while a call to this party of the session is live, or just ended: the wait for its answer stays open."""
    now = timing.now()
    recent = (now - timedelta(seconds=CALL_MAX_S)).isoformat()
    # Read every poll of `_ask` and `collect`: without a recent call the session's log is not read at all.
    if not ctx.db.execute("SELECT 1 FROM events WHERE session_id = ? AND kind = 'call.started' AND ts > ? LIMIT 1",
                          (session_id, recent)).fetchone():  # fmt: skip
        return False
    for root, *later in _calls(ctx, session_id).values():
        if root.payload.get("party") != party:
            continue
        started = root if root.kind == "call.started" else next((e for e in later if e.kind == "call.started"), None)
        if started is None or now >= started.ts + timedelta(seconds=CALL_MAX_S):
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
    if rfq.origin and rfq.destination:
        lane = f"{rfq.origin} to {rfq.destination}"
    elif rfq.origin:
        lane = f"from {rfq.origin}"
    elif rfq.destination:
        lane = f"to {rfq.destination}"
    else:
        lane = "this air freight shipment"
    parts = []
    if rfq.weight_kg and rfq.weight_kg > 0:
        parts.append(f"{rfq.weight_kg} kg")
    if rfq.volume_cbm and rfq.volume_cbm > 0:
        parts.append(f"{rfq.volume_cbm} cubic metres")
    if rfq.pieces:
        parts.append(f"{rfq.pieces} pieces")
    return lane, ", ".join(parts) or "air freight cargo"


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


def ring_order(ctx: Context, shipment_id: str) -> dict:
    """Starts a simulated order-status phone call for a specific shipment. Emits `call.ringing` so the fake phone
    screen (`/phone` or inline) rings and the laptop screen opens the live visual feedback monitor."""
    from pixel.crm import shipments
    from pixel.ui import names

    if not configured(ctx):
        raise CallError(503, "Voice calls are not set up: ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID.")
    shipment = shipments.get(ctx, shipment_id.strip())
    if shipment is None:
        raise CallError(404, f"Unknown order {shipment_id}.")
    customer_name = names.who(ctx, shipment.customer).name
    ref = f"order {shipment.id}" + (f" (booking {shipment.booking_reference})" if shipment.booking_reference else "")
    lane = f"{shipment.origin} to {shipment.destination}"
    details = f"{shipment.weight_kg} kg for {customer_name}, currently marked as {status_label(shipment.status).lower()}"
    variables = {
        "party": "order_status",
        "name": shipment.carrier,
        "reference": ref,
        "lane": lane,
        "shipment": details,
        "wanted": "a quick status update on where the shipment is right now and its expected arrival time",
        "signer": ctx.settings.signer,
    }
    order = {
        "id": shipment.id,
        "session_id": shipment.session_id,
        "customer": customer_name,
        "customer_email": shipment.customer,
        "origin": shipment.origin,
        "destination": shipment.destination,
        "weight_kg": str(shipment.weight_kg),
        "carrier": shipment.carrier,
        "booking_reference": shipment.booking_reference or "",
        "status": shipment.status,
        "status_label": status_label(shipment.status),
    }
    ringing = events.emit(
        ctx,
        "call.ringing",
        "voice",
        session_id=shipment.session_id,
        payload={
            "party": "order_status",
            "shipment_id": shipment.id,
            "name": shipment.carrier,
            "order": order,
            "variables": variables,
        },
    )
    return {
        "call_id": ringing.id,
        "order": order,
        "variables": variables,
        "phone_url": f"/phone?call_id={ringing.id}",
    }


def active_ringing(ctx: Context, call_id: int | None = None) -> dict | None:
    """Returns the pending ringing order call (matching `call_id`, or the most recent one within `RING_MAX_S`)."""
    cutoff = (timing.now() - timedelta(seconds=RING_MAX_S)).isoformat()
    if call_id is not None:
        rows = ctx.db.execute(
            "SELECT id, session_id FROM events WHERE id = ? AND kind = 'call.ringing'", (call_id,)
        ).fetchall()
    else:
        rows = ctx.db.execute(
            "SELECT id, session_id FROM events WHERE kind = 'call.ringing' AND ts > ? ORDER BY id DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
    for row in rows:
        chain = _calls(ctx, row["session_id"]).get(row["id"], [])
        if not chain:
            continue
        if any(e.kind in ("call.started", "call.ended") for e in chain[1:]):
            continue
        root = chain[0]
        return {
            "call_id": root.id,
            "session_id": root.session_id,
            "order": root.payload.get("order", {}),
            "variables": root.payload.get("variables", {}),
        }
    return None


async def accept_order(ctx: Context, call_id: int) -> dict:
    """Called when the human picks up the fake phone screen. Fetches an ElevenLabs signed URL and emits
    `call.started`."""
    if not configured(ctx):
        raise CallError(503, "Voice calls are not set up: ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID.")
    root = _call_root(ctx, call_id)
    if root.kind != "call.ringing":
        raise CallError(409, "This call cannot be picked up.")
    chain = _calls(ctx, root.session_id).get(call_id, [])
    if any(e.kind == "call.ended" for e in chain):
        raise CallError(409, "This call has already ended.")
    if any(e.kind == "call.started" for e in chain):
        raise CallError(409, "This call was already picked up.")
    signed = await _el(ctx, f"/convai/conversation/get-signed-url?agent_id={ctx.settings.elevenlabs_agent_id}")
    variables = root.payload.get("variables", {})
    events.emit(
        ctx,
        "call.started",
        "voice",
        session_id=root.session_id,
        payload={
            "call_id": call_id,
            "party": "order_status",
            "shipment_id": root.payload.get("shipment_id"),
            "name": root.payload.get("name"),
            "order": root.payload.get("order", {}),
            "variables": variables,
        },
    )
    return {
        "call_id": call_id,
        "signed_url": signed["signed_url"],
        "dynamic_variables": variables,
        "order": root.payload.get("order", {}),
    }


def record_turn(ctx: Context, call_id: int, role: str, message: str) -> None:
    """Streams one spoken turn from the phone screen to the laptop monitor via `call.turn`."""
    root = _call_root(ctx, call_id)
    text = str(message or "").strip()
    if not text:
        return
    clean_role = "agent" if role in ("ai", "agent") else "user"
    events.emit(
        ctx,
        "call.turn",
        "voice",
        session_id=root.session_id,
        payload={"call_id": call_id, "role": clean_role, "message": text[:1000]},
    )


def connected(ctx: Context, call_id: int, conversation_id: str) -> None:
    root = _call_root(ctx, call_id)
    if not re.fullmatch(r"[\w-]{1,100}", conversation_id):
        raise CallError(400, "Invalid conversation id.")
    events.emit(ctx, "call.connected", "voice", session_id=root.session_id,
                payload={"call_id": call_id, "conversation_id": conversation_id})  # fmt: skip


def ended(ctx: Context, call_id: int, error: str = "") -> None:
    root = _call_root(ctx, call_id)
    if any(e.kind == "call.ended" for e in _calls(ctx, root.session_id).get(call_id, [])):
        return
    events.emit(ctx, "call.ended", "voice", session_id=root.session_id,
                payload={"call_id": call_id} | ({"error": error[:300]} if error else {}))  # fmt: skip


def _decimal(value: object, name: str) -> Decimal:
    raw = re.sub(r"[^\d.,-]", "", str(value or "").strip())
    if "," in raw and "." in raw:
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif raw.count(",") == 1 and len(raw.split(",")[1]) <= 2:
        raw = raw.replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", "")
    try:
        number = Decimal(raw)
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


def answer_order(ctx: Context, call_id: int, fields: dict) -> dict:
    """Processes the ElevenLabs `record_order_status` tool call: updates the shipment in CRM and emits
    `shipment.updated` so the laptop screen reflects the change live."""
    from pixel.crm import shipments

    root = _call_root(ctx, call_id)
    chain = _calls(ctx, root.session_id).get(call_id, [])
    if any(e.kind == "call.ended" for e in chain):
        raise CallError(409, "This call is over.")
    shipment_id = str(root.payload.get("shipment_id") or "")
    shipment = shipments.get(ctx, shipment_id)
    if shipment is None:
        raise CallError(404, f"Unknown order {shipment_id}.")

    # Merge with any earlier update in this same call so progressive updates keep earlier fields.
    prior: dict[str, str] = {}
    for ev in chain:
        if ev.kind == "call.answer" and isinstance(ev.payload.get("fields"), dict):
            for k, v in ev.payload["fields"].items():
                if v:
                    prior[k] = str(v)

    new_status = normalize_order_status(fields.get("status") or prior.get("status", ""))
    location = str(fields.get("location") or prior.get("location") or "").strip()[:160]
    eta = str(fields.get("eta") or prior.get("eta") or "").strip()[:120]
    flight_number = str(fields.get("flight_number") or prior.get("flight_number") or "").strip()[:40]
    notes = str(fields.get("notes") or prior.get("notes") or "").strip()[:300]

    previous_status = shipment.status
    now = timing.now()
    updated = shipment.model_copy(update={"status": new_status, "updated_at": now})
    shipments.save(ctx, updated)

    merged_fields = {
        "status": new_status,
        "location": location,
        "eta": eta,
        "flight_number": flight_number,
        "notes": notes,
    }
    payload = {
        "call_id": call_id,
        "shipment_id": shipment.id,
        "previous_status": previous_status,
        "status": new_status,
        "status_label": status_label(new_status),
        "location": location,
        "eta": eta,
        "flight_number": flight_number,
        "notes": notes,
        "updated_at": now.isoformat(),
    }
    events.emit(ctx, "call.answer", "voice", session_id=root.session_id,
                payload={"call_id": call_id, "shipment_id": shipment.id, "fields": merged_fields})  # fmt: skip
    events.emit(ctx, "shipment.updated", "voice", session_id=root.session_id, payload=payload)
    return payload


def latest_order_update(ctx: Context, shipment_id: str) -> dict | None:
    """Most recent `shipment.updated` payload for a shipment, if any."""
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE kind = 'shipment.updated' AND json_extract(payload, '$.shipment_id') = ?"
        " ORDER BY id DESC LIMIT 1",
        (shipment_id,),
    ).fetchone()
    if not row:
        return None
    import json

    return json.loads(row["payload"])


def call_state(ctx: Context, call_id: int) -> dict:
    """Full live snapshot of one call (status, spoken turns, latest shipment update) for instant catch-up."""
    root = _call_root(ctx, call_id)
    chain = _calls(ctx, root.session_id).get(call_id, [])
    turns = [
        {"id": e.id, "role": e.payload.get("role", "user"), "message": e.payload.get("message", "")}
        for e in chain
        if e.kind == "call.turn"
    ]
    shipment_id = str(root.payload.get("shipment_id") or "")
    latest_update = None
    if shipment_id:
        for ev in reversed(events.for_session(ctx, root.session_id, call_id - 1)):
            if ev.kind == "shipment.updated" and ev.payload.get("call_id") == call_id:
                latest_update = ev.payload
                break
    ended_ev = next((e for e in chain if e.kind == "call.ended"), None)
    return {
        "call_id": call_id,
        "started": any(e.kind in ("call.started", "call.connected") for e in chain),
        "ended": ended_ev is not None,
        "error": ended_ev.payload.get("error", "") if ended_ev else "",
        "turns": turns,
        "latest_update": latest_update,
    }


def answer(ctx: Context, call_id: int, fields: dict) -> int | dict:
    """The agent's client tool during the call: for order-status calls updates the shipment live; for carrier/customer
    calls stores what it heard as an incoming mail in the thread and hands it to the orchestrator."""
    from pixel.core import orchestrator, poller

    root = _call_root(ctx, call_id)
    if root.payload.get("party") == "order_status":
        return answer_order(ctx, call_id, fields)
    session_id = root.session_id
    chain = _calls(ctx, session_id).get(call_id, [])
    if any(e.kind == "call.answer" for e in chain):
        raise CallError(409, "This call's answer is already recorded.")
    if timing.now() >= root.ts + timedelta(seconds=CALL_MAX_S) or any(e.kind == "call.ended" for e in chain):
        raise CallError(409, "This call is over.")
    target = Target(**{k: tuple(v) if k == "missing" else v for k, v in root.payload.items() if k != "variables"})
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
    root = _call_root(ctx, call_id)
    chain = _calls(ctx, root.session_id).get(call_id, [])
    if kept := next((e for e in chain if e.kind == "call.transcript"), None):
        return kept.payload
    conversation = next((e.payload["conversation_id"] for e in chain if e.kind == "call.connected"), None)
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
    events.emit(ctx, "call.transcript", "voice", session_id=root.session_id, payload=payload)
    return payload
