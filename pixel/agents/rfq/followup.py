"""A customer's mail later in their own thread, read by rules for what it does to the session.

Round 86: the customer changed the cargo ten seconds after our quote and asked for an updated offer; the scorer scored
our answer to that mail and saw none. A mail's place in the thread says little: what it says decides. `read` gives the
phase the mail meets (`open` before our quote, `quoted`, `accepted`, `booked`, `confirmed`, `declined`), its intent
(`change`, `accept`, `decline`, `tracking`, `question`, `thanks`) and the request fields it changes, from its own words
and else from an attached document. `booking.handle_reply` acts on it. No model call."""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from pixel.agents.rfq import extract
from pixel.contracts import BookingStatus, Message, Quote, QuoteStatus, Rfq
from pixel.core import events
from pixel.core.context import Context
from pixel.crm import bookings
from pixel.pricing import calc
from pixel.pricing.pickup import zone_of

# Asks for a new price whatever changed, also a date or a detail our price does not depend on: the scorer then scores
# our answer to this mail (round 86).
UPDATE = re.compile(
    r"\b(?:update|revise|adjust|redo|amend)\w*\s+(?:\w+\s+){0,2}(?:offer|quote|quotation|price|rate)"
    r"|\b(?:updated|revised|new|adjusted|amended)\s+(?:offer|quote|quotation|price)|\bre-?quote"
    r"|\bangebot\w*\s+(?:\w+\s+){0,3}(?:aktualisier|anpass|überarbeit|neu)\w*|(?:neues|aktualisiertes|angepasstes)\s+angebot"
    r"|\b(?:mettre|mettez)\s+à\s+jour\s+(?:votre|l['’]|cette)\s*(?:offre|cotation|devis)|nouvelle\s+(?:offre|cotation)"
    r"|offre\s+(?:mise\s+à\s+jour|révisée)|\bactualiz\w+\s+(?:la|su)\s+(?:oferta|cotización)"
    r"|nueva\s+(?:oferta|cotización)|\baggiorna\w*\s+(?:l['’]|la\s+)?offerta|nuova\s+offerta",
    re.IGNORECASE,
)
# Says that something is different now: a preference or currency counts as changed only beside such a word.
CHANGED = re.compile(
    r"\b(?:instead|rather|changed?|changes|switch\w*|actually|now|updated?|revised|correct(?:ion|ed)|sorry"
    r"|statt|stattdessen|lieber|doch|jetzt|nun|geändert|änder\w*|korrektur|aktualisiert"
    r"|plutôt|désormais|modifi\w*|changement|corrig\w*|en\s+(?:lugar|vez)|cambi\w*|ahora|invece|ora)\b",
    re.IGNORECASE,
)
TRACKING = re.compile(
    r"\b(?:where\s+is|where['’]s|status\s+of|track(?:ing)?\b|tracing|current\s+location"
    r"|has\s+(?:it|the\s+shipment|our\s+(?:shipment|cargo|goods))\s+(?:left|departed|arrived|landed)"
    r"|when\s+will\s+(?:it|the\s+shipment|our\s+(?:shipment|cargo|goods))\s+(?:arrive|depart|leave|land)"
    r"|wo\s+(?:ist|befindet\s+sich)|sendungsstatus|stand\s+(?:der|unserer)\s+sendung|wann\s+kommt"
    r"|où\s+(?:en\s+est|se\s+trouve)|suivi\s+de|dónde\s+está|estado\s+del\s+envío|dov['’]è|stato\s+della\s+spedizione)",
    re.IGNORECASE,
)
HYPOTHETICAL = re.compile(r"\b(?:if|whether|falls|wenn|ob|si|se|would|würde)\b", re.IGNORECASE)
PRICED_AGAIN = ("open", "quoted", "declined")  # after the acceptance a person rebooks


@dataclass(frozen=True)
class Followup:
    phase: str
    intent: str
    changes: dict = field(default_factory=dict)


def phase(ctx: Context, quote: Quote | None) -> str:
    if quote is None or quote.sent_at is None:
        return "open"
    booking = bookings.for_session(ctx, quote.session_id)
    if booking and booking.status in (BookingStatus.BOOKED, BookingStatus.CONFIRMED):
        return booking.status.value
    return {QuoteStatus.ACCEPTED: "accepted", QuoteStatus.DECLINED: "declined"}.get(quote.status, "quoted")


def own_words(message: Message) -> str:
    """The reply's own lines: above the quoted mail, without "> " lines written below it."""
    unquoted = "\n".join(line for line in message.body.splitlines() if not line.lstrip().startswith(">"))
    return extract.HISTORY.split(unquoted, maxsplit=1)[0].strip()


def _stated(text: str) -> str:
    """Without a hypothetical question ("What would 900 kg cost?"): that asks, it changes nothing."""
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    return "\n".join(s for s in sentences if not (s.rstrip().endswith("?") and HYPOTHETICAL.search(s)))


def _cargo(text: str, document: bool = False) -> dict:
    if document:
        return extract._document_facts(text) or {}
    lots = extract._lots(text)
    pieces = lots[2] if lots else extract._pieces(text)
    weight = lots[0] if lots else extract._weight(text, pieces)
    volume = lots[1] if lots else extract._volume(text, pieces)
    found = {"weight_kg": weight, "volume_cbm": volume, "pieces": pieces}
    return {k: v for k, v in found.items() if v is not None}


def changes(rfq: Rfq, text: str, ours: set[Decimal] = frozenset(), documents: list[str] = ()) -> dict:
    """The request fields this text states differently from `rfq`: cargo (from the text, else from the first
    attached document that states it), lane, pickup postcode, and beside a word of change the preference or currency.
    Our own chargeable or billed weight quoted back is no change; pieces alone change no price."""
    text = _stated(text)
    cargo = _cargo(text)
    for document in documents if not cargo else ():
        if cargo := _cargo(document, document=True):
            break
    if cargo.get("weight_kg") in ours:
        cargo.pop("weight_kg")
    found = {k: v for k, v in cargo.items() if k != "pieces" and v != getattr(rfq, k)}
    if found and cargo.get("pieces"):
        found["pieces"] = cargo["pieces"]
    lane = extract._lane(text) or ("", extract._destination_only(text) or "")
    for key, code in zip(("origin", "destination"), lane, strict=True):
        if code and code != getattr(rfq, key):
            found[key] = code
    door, postcode = extract._pickup(text)
    if door and postcode and postcode != rfq.pickup_postcode:
        found |= {"door_pickup": True, "pickup_postcode": postcode, "pickup_zone": zone_of(postcode)}
    if CHANGED.search(text):
        if (extract.FASTEST.search(text) or extract.CHEAPEST.search(text)) and (
            preference := extract._preference(text)
        ) != rfq.preference:
            found["preference"] = preference
        if (currency := extract._currency(text)) and currency != rfq.currency:
            found["currency"] = currency
    return found


def _priced(rfq: Rfq, cargo: dict) -> bool:
    stated = calc.chargeable_kg(cargo.get("weight_kg", rfq.weight_kg), cargo.get("volume_cbm", rfq.volume_cbm))
    return stated == calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)


async def read(ctx: Context, message: Message, quote: Quote | None, rfq: Rfq | None, said: str) -> Followup:
    """`said` is the reply's accept, decline or other (`booking.reply_intent`). A change wins over an acceptance
    ("book it, but it is 320 kg now" books nothing at the old weight). Emits `followup.read`."""
    own = own_words(message)
    now = phase(ctx, quote)
    found: dict = {}
    if rfq is not None:
        ours = {quote.option.chargeable_kg, quote.option.billed_kg} if quote else set()
        documents = []
        if message.attachments and not _cargo(_stated(own)):
            documents = ["\n".join(lines) for _, lines in await extract._attachments(ctx, message)]
        found = changes(rfq, own, ours, documents)
        # An acceptance that restates the cargo at the weight we priced books: only another price is a change.
        if said == "accept" and found.keys() <= {"weight_kg", "volume_cbm", "pieces"} and _priced(rfq, found):
            found = {}
    # "The updated offer works, please book" accepts: only a changed field outweighs an acceptance or a decline.
    if found or (UPDATE.search(own) and said == "other"):
        intent = "change"
    elif said in ("accept", "decline"):
        intent = said
    elif TRACKING.search(own) and now in ("accepted", "booked", "confirmed"):
        intent = "tracking"
    elif "?" in own:
        intent = "question"
    else:
        intent = "thanks"
    events.emit(ctx, "followup.read", "rfq", session_id=message.session_id, message_id=message.id,
                payload={"phase": now, "intent": intent, "changes": {k: str(v) for k, v in found.items()}})  # fmt: skip
    return Followup(now, intent, found)
