"""A customer's mail later in their own thread, read by rules for what it does to the session.

Round 86: the customer changed the cargo ten seconds after our quote and asked for an updated offer; the scorer scored
our answer to that mail and saw none. A mail's place in the thread says little: what it says decides. `read` gives the
phase the mail meets (`open` before our quote, `quoted`, `accepted`, `booked`, `confirmed`, `cancelled`, `declined`), its
intent
(`change`, `cancel`, `accept`, `decline`, `conditional`, `tracking`, `question`, `thanks`) and the request fields it changes, from its own
words and else from an attached document; `action` is what the session does with it (`conditional`, "book it if it stays below EUR X", books only when
`selection.within_cap` says our total meets the cap), `booking.handle_reply` does it.
No model call."""

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
    r"|\b(?:correct|rectify)\s+(?:the|your|this|our)\s+(?:\w+\s+)?(?:offer|quote|quotation|price|rate)\b"
    r"|\b(?:updated|revised|new|adjusted|amended|corrected)\s+(?:offer|quote|quotation|price)|\bre-?quote"
    r"|\bangebot\w*\s+(?:\w+\s+){0,3}(?:aktualisier|anpass|überarbeit|korrigier|neu)\w*|(?:neues|aktualisiertes|angepasstes)\s+angebot"
    r"|\bpassen\s+sie\s+(?:\w+\s+){0,3}angebot\w*\s+an\b|\bofferte\s+(?:\w+\s+){0,2}(?:aan\s*(?:te\s+)?passen|bijwerken)"
    r"|nieuwe\s+offerte"
    r"|\b(?:mettre|mettez|corriger|corrigez)\s+(?:à\s+jour\s+)?(?:votre|l['’]|cette)\s*(?:offre|cotation|devis)|nouvelle\s+(?:offre|cotation)"
    r"|offre\s+(?:mise\s+à\s+jour|révisée)|\bactualiz\w+\s+(?:la|su)\s+(?:oferta|cotización)"
    r"|nueva\s+(?:oferta|cotización)|\baggiorna\w*\s+(?:l['’]|la\s+)?offerta|nuova\s+offerta",
    re.IGNORECASE,
)
# The figure a correction replaces ("1,952 kg, not 753 kg", "statt 753 kg", "au lieu de 753 kg"): never the new one.
REPLACED = re.compile(
    rf"(?:,\s*|\s+|^)(?:and\s+|und\s+|et\s+)?(?:not|instead\s+of|rather\s+than|(?:an)?statt|nicht|au\s+lieu\s+de|non|pas"
    rf"|en\s+(?:lugar|vez)\s+de|invece\s+di)\s+(?:the\s+|der\s+|die\s+|des\s+|de\s+)?{extract.NUM}\s*(?:[^\W\d_]+\.?|m³)",
    re.IGNORECASE | re.MULTILINE,
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
# Withdraws the booking or the order itself, not a side request ("cancel our pickup slot request" is no cancellation).
CANCELS = re.compile(
    r"\bcancel\w*\s+(?:\w+\s+){0,2}(?:booking|shipment|order|reservation|it|BK-\w+)\b|(?:don['’]t|do not)\s+book\b"
    r"|\bwithdr[ae]w\w*\s+(?:\w+\s+){0,2}order|\b(?:storn\w*|annul\w*)\b|nicht\s+buchen|buchung\s+zurück\w*"
    r"|\bcancelar\s+(?:\w+\s+){0,2}(?:reserva|envío|pedido)|\bcancellare\s+(?:\w+\s+){0,2}(?:prenotazione|spedizione)",
    re.IGNORECASE,
)
# A condition with a booking word in one sentence ("If the all-in stays below EUR 400, please book it"): never a
# thank-you that needs nobody. Its cap, when it states one, is compared with our total by `selection.within_cap`.
CONDITION = re.compile(
    r"\b(?:if|as long as|provided|unless|falls|wenn|sofern|solange|si|sous\s+réserve|se)\b", re.IGNORECASE
)
BOOK_WORD = re.compile(
    r"\b(?:book\w*|proceed|go\s+ahead|buch\w*|beauftrag\w*|réserv\w*|reserv\w*|prenot\w*|confirm\w*\s+(?:the\s+)?order)\b",
    re.IGNORECASE,
)
CAP = re.compile(
    r"\b(?:(below|under|less\s+than|lower\s+than|cheaper\s+than|unter(?:halb)?|weniger\s+als|günstiger\s+als"
    r"|moins\s+de|en\s+dessous\s+de|inférieur\s+à|por\s+debajo\s+de|menos\s+de|sotto)"
    r"|(up\s+to|at\s+most|max(?:imum)?\.?|not\s+(?:more|higher)\s+than|no\s+more\s+than|(?:does\s+not|doesn['’]t)\s+exceed"
    r"|höchstens|maximal|bis\s+(?:zu\s+)?|nicht\s+mehr\s+als|au\s+maximum|pas\s+plus\s+de|jusqu['’]à"
    r"|match|within(?:\s+(?:a|our)\s+budget)?|budget|im\s+rahmen\s+von))"
    r"\s*(?:of\s+|von\s+|de\s+)?(?!\d{4}-\d{2})(?=[€$\d]|EUR|USD|GBP|CHF)",
    re.IGNORECASE,
)
SYMBOL = {"€": "EUR", "$": "USD"}
HYPOTHETICAL = re.compile(r"\b(?:if|whether|falls|wenn|ob|si|se|would|würde)\b", re.IGNORECASE)
QUOTE_CHECK = re.compile(
    r"(?:^|(?<=[.!?])\s+|\n)\s*(?:is\s+(?:this|that)|please\s+confirm|(?:can|could|would)\s+you\s+(?:please\s+)?confirm)\b"
    r"[^.!?\n]*\b(?:offer|quote|quotation|price|rate)\b",
    re.IGNORECASE,
)
PRICED_AGAIN = ("open", "quoted", "declined")  # after the acceptance a person rebooks
BOOKED = ("accepted", "booked", "confirmed")


@dataclass(frozen=True)
class Followup:
    phase: str
    intent: str
    changes: dict = field(default_factory=dict)
    cap: tuple[Decimal, str | None, bool] | None = None  # a conditional acceptance's (amount, currency, strict)


def phase(ctx: Context, quote: Quote | None) -> str:
    if quote is None or quote.sent_at is None:
        return "open"
    booking = bookings.for_session(ctx, quote.session_id)
    if booking and booking.status in (BookingStatus.BOOKED, BookingStatus.CONFIRMED, BookingStatus.CANCELLED):
        return booking.status.value
    return {QuoteStatus.ACCEPTED: "accepted", QuoteStatus.DECLINED: "declined"}.get(quote.status, "quoted")


def own_words(message: Message) -> str:
    """The reply's own lines: above the quoted mail, without "> " lines written below it."""
    unquoted = "\n".join(line for line in message.body.splitlines() if not line.lstrip().startswith(">"))
    return extract.HISTORY.split(unquoted, maxsplit=1)[0].strip()


def _stated(text: str) -> str:
    """Only stated cargo facts count. A hypothetical or a request to confirm the quote checks its basis."""
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    return "\n".join(
        s
        for s in sentences
        if not (s.rstrip().endswith("?") and HYPOTHETICAL.search(s))
        and not (QUOTE_CHECK.search(s) and not CHANGED.search(s))
    )


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
    Our own chargeable or billed weight quoted back is no change."""
    text = REPLACED.sub("", _stated(text))
    cargo = _cargo(text)
    for document in documents if not cargo else ():
        if cargo := _cargo(document, document=True):
            break
    if cargo.get("weight_kg") in ours:
        cargo.pop("weight_kg")
    found = {k: v for k, v in cargo.items() if v != getattr(rfq, k)}
    lane = extract._lane(text) or ("", extract._destination_only(text, truck_leg=False) or "")
    for key, code in zip(("origin", "destination"), lane, strict=True):
        # a destination where the cargo departs is the truck's airport ("Wir bringen die Luftfracht zum Flughafen MUC")
        if code and code != getattr(rfq, key) and not (key == "destination" and code == (lane[0] or rfq.origin)):
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


# "2000 EUR or less": a cap written after its amount.
CAP_AFTER = re.compile(
    rf"(?:(?:EUR|USD|GBP|CHF|€|\$)\s*{extract.NUM}|{extract.NUM}\s*(?:EUR|USD|GBP|CHF|€|\$|euros?)?)"
    r"\s+(?:or\s+less|or\s+below|oder\s+weniger|max(?:imum)?\b|at\s+most|tops)",
    re.IGNORECASE,
)


def conditional(text: str) -> tuple[bool, tuple[Decimal, str | None, bool] | None]:
    """Whether a sentence books on a condition, and the price cap it names: (amount, currency or None, strict)."""
    for sentence in re.split(r"(?<=[.!?])(?<!\bmax\.)(?<!\bca\.)(?<!\bapprox\.)\s+|\n", text):
        if not (CONDITION.search(sentence) and BOOK_WORD.search(sentence)):
            continue
        for m in CAP.finditer(sentence):
            money = extract.MONEY.match(sentence, m.end())
            raw, unit = (money.group(2) or money.group(4), money.group(1) or money.group(6)) if money else (None, None)
            if raw is None and (bare := re.match(extract.NUM, sentence[m.end() :])):
                raw = bare.group(1)
            amount = extract.number(raw) if raw else None
            if money and (money.group(3) or money.group(5)) and amount is not None:
                amount *= 1000
            if amount:
                currency = SYMBOL.get(unit, unit.upper() if unit else None)
                return True, (amount, currency, m.group(1) is not None)
        if (after := CAP_AFTER.search(sentence)) and (amount := extract.number(after.group(1) or after.group(2))):
            unit = re.search(r"EUR|USD|GBP|CHF|€|\$|euro", after.group(0), re.IGNORECASE)
            currency = unit and {"euro": "EUR", **SYMBOL}.get(unit.group(0).lower(), unit.group(0).upper())
            return True, (amount, currency, False)
        return True, None
    return False, None


def _priced(rfq: Rfq, cargo: dict) -> bool:
    stated = calc.chargeable_kg(cargo.get("weight_kg", rfq.weight_kg), cargo.get("volume_cbm", rfq.volume_cbm))
    return stated == calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)


def action(read: Followup) -> str:
    """What the session does with the mail (`booking.handle_reply`): requote, track, cancel, decline, book, rebook (an
    acceptance again: only a replay of the first resumes its booking), nothing, or person."""
    phase, intent = read.phase, read.intent
    if intent == "change":
        return "requote" if phase in PRICED_AGAIN else "person"
    if intent == "tracking":
        return "track"
    if intent == "cancel":
        return {"quoted": "decline", "declined": "nothing", "cancelled": "nothing"}.get(
            phase, "cancel" if phase in BOOKED else "person"
        )
    if intent == "accept":
        return {"quoted": "book", "declined": "book"}.get(phase, "rebook" if phase in BOOKED else "person")
    if intent == "decline":
        return {"quoted": "decline", "declined": "decline"}.get(phase, "person")
    if intent == "conditional":
        return "book_if" if phase in ("quoted", "declined") else "person"
    if intent == "thanks" and phase != "open":
        return "nothing"
    return "person"


async def read(ctx: Context, message: Message, quote: Quote | None, rfq: Rfq | None, said: str) -> Followup:
    """`said` is the reply's accept, decline or other (`booking.reply_intent`). A change wins over an acceptance
    ("book it, but it is 320 kg now" books nothing at the old weight). Emits `followup.read`."""
    own = own_words(message)
    now = phase(ctx, quote)
    found: dict = {}
    cap = None
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
    elif CANCELS.search(own):  # "bitte stornieren, der Auftrag wurde zurückgezogen": "Auftrag" is no acceptance here
        intent = "cancel"
    # A cap outweighs an acceptance ("falls … unter 3.200 € bleibt, bitte buchen" reads as one after the split at
    # "3."); a condition without a cap never does ("Bitte buchen Sie. Wenn die Buchung bestätigt ist, …" books).
    elif (booked_if := conditional(own))[0] and (booked_if[1] is not None or said not in ("accept", "decline")):
        intent, cap = "conditional", booked_if[1]
    elif said in ("accept", "decline"):
        intent = said
    elif TRACKING.search(own) and now in BOOKED:
        intent = "tracking"
    elif "?" in own or QUOTE_CHECK.search(own):
        intent = "question"
    else:
        intent = "thanks"
    events.emit(ctx, "followup.read", "rfq", session_id=message.session_id, message_id=message.id,
                payload={"phase": now, "intent": intent, "changes": {k: str(v) for k, v in found.items()},
                         **({"cap": [str(cap[0]), cap[1], cap[2]]} if cap else {})})  # fmt: skip
    return Followup(now, intent, found, cap)
