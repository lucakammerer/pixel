import re

from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.contracts import MailKind, Message
from pixel.core.context import Context
from pixel.core.events import emit
from pixel.crm import bookings

TARIFF_FILES = ("pdf", "xlsx", "xls", "csv", "txt", "png", "jpg", "jpeg", "docx")
TARIFF_NAME = re.compile(r"tariff|tarif|rate|preis|price")
BOOKING_CONFIRMED = re.compile(r"\bbook(ing)?\b.*\bconfirm|\bconfirm\w*\b.*\bbook", re.IGNORECASE)
BOOKED = re.compile(r"\bstatus:?\s*booked\b|\bconfirmed\b|\bbooking reference\b", re.IGNORECASE)
REFUSED = re.compile(
    r"\bnot\s+(?:be(?:en)?\s+)?confirmed\b|\b(?:cannot|can't|unable to|could not)\s+(?:confirm|book|accept)"
    r"|\bno (?:space|capacity)\b|\b(?:declined|rejected|cancell?ed)\b",
    re.IGNORECASE,
)
CONFIRMED_SUBJECT = re.compile(r"\bbooking\s+(\S+)\s+(?:is\s+)?confirmed", re.IGNORECASE)
TEAM_REFERENCE = re.compile(r"\bPX-[0-9A-F]{6}\b")  # crm/shipments.py, sent with every booking
WEIGHT = re.compile(r"\d\s*(kg|kgs|kilo|cbm|m3|m³|lbs?|pounds?|t|tons?|tonnes?|tonnen)\b", re.IGNORECASE)
LANE = re.compile(r"\b[A-Z]{3}\b.{0,40}\b[A-Z]{3}\b")

MODEL_KINDS = (MailKind.CUSTOMER_RFQ, MailKind.CUSTOMER_REPLY, MailKind.TRACKING_QUESTION, MailKind.OTHER)
SYSTEM = (
    "You sort mail reaching an air freight forwarder. Answer with the kind: customer_rfq (asks for a price to ship "
    "something), customer_reply (answers a quote: accepts, declines, asks about it), tracking_question (asks where "
    "a shipment is), other."
)


def _carrier_kind(ctx: Context, address: str) -> str | None:
    row = ctx.db.execute("SELECT kind FROM carriers WHERE lower(email) = ?", (address,)).fetchone()
    return row["kind"] if row else None


def _answers(ctx: Context, message: Message) -> list[str]:
    """Purposes of our own mails this one replies to, nearest first."""
    ids = [i for i in (message.in_reply_to, *reversed(message.references)) if i is not None]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = ctx.db.execute(
        f"SELECT id, purpose FROM messages WHERE direction = 'out' AND id IN ({marks})", ids
    ).fetchall()
    purposes = {r["id"]: r["purpose"] or "" for r in rows}
    return [purposes[i] for i in ids if i in purposes]


def _customer_of(ctx: Context, session_id: int) -> str | None:
    row = ctx.db.execute("SELECT customer FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row["customer"] if row else None


def _lookalike(ctx: Context, sender: str, before: int) -> bool:
    """Round 105: "our mailbox is down, send the price to this address" from nordlicht.audio@ beside the customer
    nordlicht-audio@. An address that differs from a known customer's only by its dots, dashes or underscores is not
    that customer: it gets no mail from us, neither a question nor a price."""
    def plain(address: str) -> str:
        local, _, domain = address.lower().partition("@")
        return re.sub(r"[.\-_+]", "", local) + "@" + domain

    known = ctx.db.execute("SELECT DISTINCT sender FROM messages WHERE kind = ? AND sender <> ? AND id < ?",
                           (MailKind.CUSTOMER_RFQ.value, sender, before)).fetchall()  # fmt: skip
    return any(plain(row[0]) == plain(sender) for row in known)


def rules(ctx: Context, message: Message) -> tuple[MailKind | None, str]:
    """The kind and the rule that decided, or (None, why) when the rules cannot place the mail."""
    sender = message.sender
    in_thread = message.in_reply_to is not None or bool(message.references)
    customer = _customer_of(ctx, message.session_id)
    if sender == TRACKING:
        return MailKind.TRACKING_QUESTION, "sender:tracking"
    if sender == RATEDESK:
        return MailKind.RATEDESK_REPLY, "sender:ratedesk"
    if not in_thread and _lookalike(ctx, sender, message.id):
        return MailKind.OTHER, "sender:lookalike"
    if in_thread and customer and sender == customer:
        # The answer to our question for a missing fact is read by the request's own handler, which waits for it.
        if _answers(ctx, message)[:1] == ["question"]:
            return MailKind.OTHER, "thread:question"
        return MailKind.CUSTOMER_REPLY, "thread:customer"
    # A customer may write "booking" and "confirmed" too; a confirmation comes from an airline or names our reference.
    # An airline's operations desk may confirm from an address the carrier list does not hold: a booking reference
    # we hold in the subject is enough ("Booking BK-… confirmed"), else a new thread with a lane reads as a request.
    held = CONFIRMED_SUBJECT.search(message.subject)
    confirms = (
        _carrier_kind(ctx, sender)
        or TEAM_REFERENCE.search(message.body)
        or (held and bookings.by_reference(ctx, held.group(1).strip(".,:;")))
    )
    if sender != customer and BOOKING_CONFIRMED.search(message.subject) and confirms:
        return MailKind.BOOKING_CONFIRMATION, "subject:booking_confirmed"
    purposes = _answers(ctx, message)
    if any(p.startswith("bid") for p in purposes):
        return MailKind.CARRIER_BID, "thread:bid_request"
    # Round 27: the airline's answer to our booking by mail ("Booking reference: BK-…, Status: booked") read as a bid.
    # Its confirmation may stand only in an attachment (read as a tariff before). A refusal, in the mail or the
    # attachment, goes the same way: `booking.handle_confirmation` then fails the booking for a person instead of
    # leaving it requested.
    if (
        purposes[:1] == ["booking"]
        and sender != customer
        and (BOOKED.search(message.body) or message.attachments or REFUSED.search(message.body))
    ):
        return MailKind.BOOKING_CONFIRMATION, "thread:booking_request"
    carrier = _carrier_kind(ctx, sender)
    files = [a.filename.lower() for a in message.attachments if a.filename.rsplit(".", 1)[-1].lower() in TARIFF_FILES]
    text = f"{message.subject}\n{message.body}"
    asks = bool(WEIGHT.search(text) or LANE.search(text))
    # A customer attaches files too (a packing list): a file makes a tariff only from a carrier, or when a mail
    # that asks for nothing brings a file named like one.
    if files and (carrier or (not in_thread and not asks and any(TARIFF_NAME.search(f) for f in files))):
        return MailKind.TARIFF, "attachment:tariff"
    if carrier:
        return MailKind.CARRIER_BID, "sender:carrier"
    if any(p.endswith("quote") for p in purposes):
        return MailKind.CUSTOMER_REPLY, "thread:quote"
    if not in_thread and asks:
        return MailKind.CUSTOMER_RFQ, "new_thread"
    # A colleague writing in the customer's thread before our quote: `followup.read` reads what it says.
    if in_thread and customer:
        return MailKind.CUSTOMER_REPLY, "thread:customer_session"
    return None, "new_thread_unclear" if not in_thread else "unknown_thread"


async def classify(ctx: Context, message: Message) -> MailKind:
    """Rules first: sender (desk, tracking, carrier list), thread (reply to a bid request, to a quote),
    attachments (tariff). A model call only for a customer mail the rules cannot place."""
    kind, rule = rules(ctx, message)
    by = "rules"
    if kind is None:
        by = "model"
        kind = await _ask_model(ctx, message, rule)
    emit(ctx, "route", "router", session_id=message.session_id, message_id=message.id,
         payload={"kind": kind, "by": by, "rule": rule})  # fmt: skip
    return kind


async def _ask_model(ctx: Context, message: Message, rule: str) -> MailKind:
    # A new thread nobody else claims is most likely a request; missing one costs the whole request (R1).
    default = MailKind.CUSTOMER_RFQ if rule == "new_thread_unclear" else MailKind.OTHER
    from pixel import llm

    try:
        result = await llm.ask_json(
            ctx,
            purpose="route",
            system=SYSTEM,
            user=f"Subject: {message.subject}\n\n{message.body[:2000]}",
            schema={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": [k.value for k in MODEL_KINDS]}},
                "required": ["kind"],
                "additionalProperties": False,
            },
            session_id=message.session_id,
            max_output_tokens=20,
        )
        return MailKind(result.data["kind"])
    except Exception as e:  # noqa: BLE001
        emit(ctx, "error", "router", session_id=message.session_id, message_id=message.id,
             payload={"where": "route_model", "error": f"{type(e).__name__}: {e}", "default": default})  # fmt: skip
        return default
