import asyncio
import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from pixel import llm
from pixel.contracts import Message, Preference, Rfq
from pixel.core import events, outbox, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import store
from pixel.pricing.pickup import zone_of

CURRENCIES = {"EUR", "USD", "GBP", "CHF", "CNY", "JPY", "HKD", "SGD", "AUD", "CAD", "INR", "AED"}
NOT_AIRPORTS = CURRENCIES | {
    "CBM", "RFQ", "KGS", "PCS", "ETA", "ETD", "AWB", "DAP", "EXW", "FCA", "CPT", "CIP", "DDP", "FOB", "CIF",
    "VAT", "USA", "PDF", "ASAP", "DGR", "UTC", "CET",
}  # fmt: skip

CODE = r"\b([A-Z]{3})\b"
ARROW = r"(?:->|→|–|—|-|>|(?i:\bto\b)|(?i:\bnach\b))"
PAIR = re.compile(rf"{CODE}\s*{ARROW}\s*{CODE}")
PAIR_NAMED = re.compile(rf"\(([A-Z]{{3}})\)[^()\n]{{0,60}}?{ARROW}[^()\n]{{0,60}}?\(([A-Z]{{3}})\)")
AIRPORT_WORD = r"(?:\s+(?:airport|flughafen))?"
ORIGIN_LABEL = re.compile(
    rf"(?i:\b(?:origin|from|von|ab|departure|abgangsflughafen)\b){AIRPORT_WORD}\s*:?[^\n:]{{0,30}}?{CODE}"
)
DEST_LABEL = re.compile(rf"(?i:\b(?:destination|to|nach|bis|zielflughafen)\b){AIRPORT_WORD}\s*:?[^\n:]{{0,30}}?{CODE}")

# "1 250 kg": a space (also a no-break one) between thousands, not after a date's or a range's digits.
NUM = r"((?<![\d.,\-/])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d.,'])|\d[\d.,']*)"
OTHER_UNIT = r"\s*(?i:lbs?|pounds?|t(?![-\w])|tons?|tonnes?|tonnen)"
WEIGHT = (
    re.compile(rf"{NUM}\s*(?i:kgs?|kilos?|kilograms?|kilogramm)\b"),
    re.compile(
        rf"(?i:\b(?:gross weight|weight|gewicht|bruttogewicht)(?:\s*\(kgs?\))?)\s*:?\s*{NUM}(?![\d.,']|{OTHER_UNIT}\b)"
    ),
)
POUNDS = (re.compile(rf"{NUM}\s*(?i:lbs?|pounds?)\b"),)
TONNES = (re.compile(rf"{NUM}\s*(?i:t(?![-\w])|tons?|tonnes?|tonnen)"),)
VOLUME = (
    re.compile(rf"{NUM}\s*(?:(?i:cbm|cubic met\w*|kubikmeter)\b|m3\b|m³)"),
    re.compile(rf"(?i:\b(?:cbm|volume|volumen)(?:\s*\((?:cbm|m3|m³)\))?)\s*:?\s*{NUM}"),
)
DIMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m|inch(?:es)?|in(?=\s*(?:$|[^\w\s])))?\b",  # "in total": a word, not inches
    re.IGNORECASE,
)
PER_M = {"mm": Decimal(1000), "cm": Decimal(100), "m": Decimal(1), "in": 1 / Decimal("0.0254")}


def _per_m(unit: str | None) -> Decimal:
    """Units of one metre: dimensions without a unit are centimetres; "48x40x50 in" are inches."""
    unit = (unit or "cm").lower()
    return PER_M.get(unit, PER_M["in"])


LOT_LINE = r"(?im)^\s*(?:[-*•]\s*(?=\d)|(?:lot|position|pos\.?|item|los)\s*\d+\s*[:.)-])"
LOT = re.compile(
    r"(?im)^\s*(?:[-*•]|(?:lot|position|pos\.?|item|los)\s*\d+\s*[:.)-])\s*(\d+)\s+"
    r"(?!(?:kgs?|kilos?|lbs?|cbm|cm|mm|m3|m)\b)[^\W\d](.+)$"
)
EACH = r"each|apiece|per\s+(?:piece|unit|carton|case|drum|pallet|box|crate)|je|pro\s+st(?:ü|ue)ck"
ALL = r"together|in\s+total|total|combined|altogether|zusammen|gesamt|insgesamt"
MODE = rf"(?:\b(?P<each>{EACH})|\b(?P<all>{ALL}))?\s*"
PER_PIECE = rf"(?:\b(?:{EACH})\b|(?<!\w)à)"
EACH_WEIGHT = re.compile(
    rf"{PER_PIECE}\s*(?:of\s+)?{NUM}\s*(?:kgs?|kilos?|lbs?|pounds?)\b|{NUM}\s*(?:kgs?|kilos?|lbs?|pounds?)\s+(?:gross\s+)?{PER_PIECE}",
    re.IGNORECASE,
)
EACH_VOLUME = re.compile(rf"{PER_PIECE}\s*{NUM}\s*(?:cbm|m3|m³)|{NUM}\s*(?:cbm|m3|m³)\s+{PER_PIECE}", re.IGNORECASE)
LOT_WEIGHT = re.compile(rf"{MODE}{NUM}\s*(?:kgs?|kilos?)\b(?:\s*(?P<kind>gross|brutto|net|netto))?", re.IGNORECASE)
LOT_VOLUME = re.compile(rf"{MODE}{NUM}\s*(?:cbm\b|m3\b|m³)", re.IGNORECASE)
LOT_DIMS = re.compile(MODE + DIMS.pattern, re.IGNORECASE)
PIECES = re.compile(
    r"(\d+)\s*(?:pcs|pieces?|pallets?|skids?|crates?|cases|drums?|colli|packages|cartons|boxes|stück|stk|packstücke"
    r"|paletten|kartons|kisten)\b",
    re.IGNORECASE,
)
NOT_URGENT = re.compile(
    r"\b(?:not\s+(?:urgent|in\s+a\s+hurry|time[\s-]critical)|no\s+(?:rush|hurry)|nicht\s+(?:eilig|dringend)|keine\s+eile"
    r"|price\s+(?:matters|counts|is)\s+more\s+than\s+(?:speed|time)|preis\s+ist\s+wichtiger"
    r"|(?:not|nicht)\s+(?:\w+\s+){0,4}?(?:the\s+|das\s+|die\s+)?(?:fastest|quickest|schnellst\w*))\b",
    re.IGNORECASE,
)
FASTEST = re.compile(r"\b(?:fastest|quickest|schnellst\w*)\b", re.IGNORECASE)
CHEAPEST = re.compile(r"\b(?:cheapest|lowest|most economical|günstigst\w*|billigst\w*)\b", re.IGNORECASE)
HURRY = re.compile(
    r"\b(?:urgent\w*|asap|express\s+(?:service|freight|delivery|option|shipment)|service\s*:\s*(?:express|priority)|as soon as possible|as (?:quickly|fast) "
    r"as possible|time[\s-]critical|speed\s+(?:matters|counts|is)\s+more|so schnell wie möglich|eilig\w*"
    r"|dringend\w*)\b",
    re.IGNORECASE,
)
DOOR = re.compile(
    r"door[\s-]*to[\s-]*(?:port|airport)|pick[\s-]?up|truck\w* from|from our (?:site|warehouse|premises|plant|factory)"
    r"|collection from|collect from|abhol\w*|ab werk|ab haus",
    re.IGNORECASE,
)
OWN_DELIVERY = re.compile(
    r"\b(?:deliver|bring|drop)\w*\b[^.\n]{0,60}\b(?:ourselves|selbst)\b"
    r"|\bno\s+(?:pick[\s-]?up|collection)\s+(?:is\s+)?(?:needed|required|necessary)\b"
    r"|\bkeine\s+abholung\s+(?:nötig|notwendig|erforderlich)\b",
    re.IGNORECASE,
)
POSTCODE = re.compile(
    r"(?<![\d.,])(?<!No\. )(?<!Nr\. )(?<!#)(?:D-)?(\d{5})(?![\d.,]?\d)(?!\s*(?:kg|kgs|cbm|m3|m³|pcs)\b)", re.IGNORECASE
)
# "Dangerous goods: UN1133, Class 3" in a packing list, also when the mail itself says "non-dangerous".
DANGEROUS = re.compile(
    r"(?<!non-)(?<!non )(?<!no )(?<!not )(?<!kein )(?<!keine )(?<!kein)"
    r"\b(?:dangerous goods|hazardous goods|dgr|gefahrgut)\b\s*[:|]?\s*"
    r"(?!no\b|none\b|n/?a\b|nein\b|kein\w*|non\b|not\b|-|$)(\w.*)|(.*\bUN\s?\d{4}\b.*)",
    re.IGNORECASE,
)
DG_DOCUMENTS = "dangerous_goods_documents"
CURRENCY_ASK = re.compile(r"(?i:\b(?:in|currency:?|währung:?))\s+([A-Z]{3})\b")
GERMAN = {"und", "der", "die", "das", "bitte", "mit", "für", "von", "nach", "wir", "ich", "ein", "eine", "angebot",
          "grüße", "gewicht", "ist", "zu", "sie", "ihr", "unser", "sendung", "danke"}  # fmt: skip
ENGLISH = {"the", "and", "please", "for", "with", "we", "to", "a", "quote", "regards", "weight", "is", "of", "our",
           "you", "your", "shipment", "thanks"}  # fmt: skip

SYSTEM = (
    "You read an air freight customer's request for a quote and return its facts as JSON. "
    "origin and destination: 3-letter IATA airport codes, null when not stated. weight_kg: gross weight in kg "
    "as a plain number string, volume_cbm: volume in cubic metres as a plain number string, null when not stated. "
    "pieces: integer or null. preference: fastest only when the customer asks for speed, else cheapest. "
    "door_pickup: true when the goods must be collected from the customer's address. pickup_postcode: that "
    "address's postcode or null. currency: only when the customer asks for one, else null. language: ISO 639-1 "
    "code of the mail. lots: only when the mail lists two or more piece groups, one entry per group exactly as "
    "written: pieces, weight and its unit, weight_per_piece true when that weight is for one piece of the group, "
    'volume_cbm, volume_per_piece likewise, dimensions of one piece as written ("120 x 80 x 100 cm") or null; '
    "net weights are never wanted; else an empty list. Never calculate or guess. "
    "Also write three short pieces for our reply, in the mail's language and tone (German: formal Sie): first_name, "
    "the customer's first name only if they signed with it, else null; intro, one or two warm sentences that show "
    "you read their mail (the goods, urgency, timing or a question they asked, in words); closing, one short "
    "friendly last line that is not a call to action, or null. These three never contain digits, prices, dates, "
    "airlines, airports or places, and never greet or sign."
)
NULLABLE_STR = {"type": ["string", "null"]}
LOT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pieces": {"type": ["integer", "null"]},
        "weight": NULLABLE_STR,
        "weight_unit": {"type": "string", "enum": ["kg", "lb"]},
        "weight_per_piece": {"type": "boolean"},
        "volume_cbm": NULLABLE_STR,
        "volume_per_piece": {"type": "boolean"},
        "dimensions": NULLABLE_STR,
    },
    "required": ["pieces", "weight", "weight_unit", "weight_per_piece", "volume_cbm", "volume_per_piece", "dimensions"],
}
LB = Decimal("0.45359237")
PROSE = ("first_name", "intro", "closing")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "origin": NULLABLE_STR,
        "destination": NULLABLE_STR,
        "weight_kg": NULLABLE_STR,
        "volume_cbm": NULLABLE_STR,
        "pieces": {"type": ["integer", "null"]},
        "lots": {"type": "array", "items": LOT_SCHEMA},
        "preference": {"type": "string", "enum": ["cheapest", "fastest"]},
        "door_pickup": {"type": "boolean"},
        "pickup_postcode": NULLABLE_STR,
        "currency": NULLABLE_STR,
        "language": {"type": "string"},
        "first_name": NULLABLE_STR,
        "intro": NULLABLE_STR,
        "closing": NULLABLE_STR,
    },
    "required": ["origin", "destination", "weight_kg", "volume_cbm", "pieces", "lots", "preference", "door_pickup",
                 "pickup_postcode", "currency", "language", "first_name", "intro", "closing"],
}  # fmt: skip


def number(text: str, thousands: bool = True) -> Decimal | None:
    s = re.sub(r"['\s\u00a0\u202f]", "", text).rstrip(".,")
    if "." in s and "," in s:
        decimal_sep = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if decimal_sep == "." else ".", "").replace(",", ".")
    elif thousands and re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = s.replace(".", "").replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _airport(code: str) -> str | None:
    return None if code in NOT_AIRPORTS else code


def _lane(text: str) -> tuple[str, str] | None:
    pairs = {
        (o, d)
        for pattern in (PAIR, PAIR_NAMED)
        for o, d in pattern.findall(text)
        if _airport(o) and _airport(d) and o != d
    }
    if len(pairs) == 1:
        return pairs.pop()
    if pairs:
        return None
    origins = {c for c in ORIGIN_LABEL.findall(text) if _airport(c)}
    destinations = {c for c in DEST_LABEL.findall(text) if _airport(c)}
    if len(origins) == 1 and len(destinations) == 1 and origins != destinations:
        return origins.pop(), destinations.pop()
    return None


def _destination_only(text: str) -> str | None:
    """The one destination of a mail that names no origin airport (door-to-port)."""
    if any(_airport(c) for c in ORIGIN_LABEL.findall(text)):
        return None
    found = {c for c in DEST_LABEL.findall(text) if _airport(c)}
    return found.pop() if len(found) == 1 else None


def _unique(patterns: tuple[re.Pattern, ...], text: str, thousands: bool) -> Decimal | None:
    values = {v for p in patterns for m in p.findall(text) if (v := number(m, thousands)) is not None and v > 0}
    return values.pop() if len(values) == 1 else None


def _pieces(text: str) -> int | None:
    found = {int(m) for m in PIECES.findall(text)}
    return found.pop() if len(found) == 1 else None


def _weight(text: str, pieces: int | None) -> Decimal | None:
    """The one gross weight; a weight per piece ("4 cartons, each 48 kg") times the pieces, or None without them."""
    weight = _unique(WEIGHT, text, thousands=True)
    if weight is None and not any(p.search(text) for p in WEIGHT):  # in pounds or tonnes only: code converts
        pounds, tonnes = _unique(POUNDS, text, thousands=True), _unique(TONNES, text, thousands=False)
        if (pounds is None) != (tonnes is None):
            weight = pounds * LB if pounds is not None else tonnes * 1000
    if weight is None or not EACH_WEIGHT.search(text):
        return weight
    return weight * pieces if pieces else None


def _volume(text: str, pieces: int | None) -> Decimal | None:
    stated = _unique(VOLUME, text, thousands=False)
    if stated is not None and EACH_VOLUME.search(text):
        return stated * pieces if pieces else None
    if stated is not None:
        return stated
    dims = DIMS.findall(text)
    if len(dims) != 1:
        return None
    *sides, unit = dims[0]
    per_m = _per_m(unit)
    volume = Decimal(1)
    for side in sides:
        volume *= number(side, thousands=False) / per_m
    return volume * (pieces or 1)


def _lots(text: str) -> tuple[Decimal, Decimal, int] | None:
    """Gross weight, volume and pieces of a request that lists its shipment lot by lot ("Lot 2: 4 cartons, each
    48 kg gross, each 110 x 110 x 52 cm"). A figure is per piece with "each", else for the whole lot ("9 pieces,
    345 kg gross" was 345 kg to the scorer); code adds them up. None unless there are several lots, each certain."""
    lots = LOT.findall(text)
    if len(lots) != len(re.findall(LOT_LINE, text)):  # a lot we cannot read would be left out of the sum
        return None
    weight, volume, pieces = Decimal(0), Decimal(0), 0
    for count, rest in lots:
        n = int(count)
        weights = [m for m in LOT_WEIGHT.finditer(rest) if (m["kind"] or "").lower() not in ("net", "netto")]
        stated, dims = LOT_VOLUME.search(rest), LOT_DIMS.search(rest)
        if len(weights) != 1 or not (stated or dims):
            return None
        kg = number(weights[0].group(3))
        if stated:
            cbm = number(stated.group(3), thousands=False) * (n if stated["each"] else 1)
        else:
            *sides, unit = dims.groups()[2:]
            per_m = _per_m(unit)
            cbm = Decimal(1 if dims["all"] else n)
            for side in sides:
                cbm *= number(side, thousands=False) / per_m
        if kg is None or cbm is None:
            return None
        weight += kg * (n if weights[0]["each"] else 1)
        volume += cbm
        pieces += n
    return (weight, volume, pieces) if len(lots) > 1 else None


def _pickup(text: str) -> tuple[bool, str | None]:
    if not DOOR.search(text) or OWN_DELIVERY.search(text):
        return False, None
    codes = set(POSTCODE.findall(text))
    return True, codes.pop() if len(codes) == 1 else None


def _currency(text: str) -> str | None:
    asked = {c for c in CURRENCY_ASK.findall(text) if c in CURRENCIES}
    return asked.pop() if len(asked) == 1 else None


def _language(text: str) -> str:
    words = re.findall(r"[a-zäöüß]+", text.lower())
    return "de" if sum(w in GERMAN for w in words) > sum(w in ENGLISH for w in words) else "en"


def _preference(text: str) -> Preference:
    """A superlative the customer wrote decides; without one, a hurry they describe means fastest."""
    text = NOT_URGENT.sub("", text)
    if FASTEST.search(text):
        return Preference.FASTEST
    if CHEAPEST.search(text) or not HURRY.search(text):
        return Preference.CHEAPEST
    return Preference.FASTEST


# A customer ruling an airline out (round 42: "Please do not offer <airline> (<code>) for this shipment or for any of
# our future inquiries", quoted anyway and rejected as excluded_airline).
RULED_OUT = re.compile(
    r"\b(?:not|never|exclud\w*|avoid\w*|without|except|other than"
    r"|nicht|kein\w*|ohne|au(?:ß|ss)er|vermeid\w*|ausschlie(?:ß|ss)\w*)\b",
    re.IGNORECASE,
)
LASTING = re.compile(
    r"\b(?:future|from now on|always|again|any of our|all of our|all our"
    r"|künftig|zukünftig|ab sofort|generell|grundsätzlich|immer)\b",
    re.IGNORECASE,
)
AIRLINE_WORD = re.compile(r"\s+(?:air\s+cargo|air\s+freight|airways|airlines?|air|cargo)$", re.IGNORECASE)
CODE_IN_BRACKETS = re.compile(r"\(([A-Z0-9]{2})\)")


def _calls(name: str) -> set[str]:
    """How a customer may write an airline: in full, or without its trailing "Air", "Cargo" or "Airways"."""
    short = AIRLINE_WORD.sub("", name)
    return {name.casefold()} | ({short.casefold()} if len(short) >= 4 else set())


def excluded_airlines(text: str, airlines: set[str]) -> tuple[tuple[str, ...], bool]:
    """The airlines a sentence with a negation names, from `airlines`, plus a two-letter code in brackets in it; and
    whether the customer means later requests too. A negation that names no known airline rules nothing out."""
    found, lasting = set(), False
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not RULED_OUT.search(sentence):
            continue
        lowered = sentence.casefold()
        names = {a for a in airlines if any(re.search(rf"(?<!\w){re.escape(c)}(?!\w)", lowered) for c in _calls(a))}
        if names:
            found |= names | set(CODE_IN_BRACKETS.findall(sentence))
            lasting = lasting or bool(LASTING.search(sentence))
    return tuple(sorted(found)), lasting


def _ruled_out(ctx: Context, rfq: Rfq, text: str) -> Rfq:
    """The request with the airlines this mail rules out and those the customer ruled out for good before. A lasting
    exclusion is kept as a `customer.excludes` event."""
    if ctx is None:  # the offline benchmark reads without a store
        return rfq
    names, lasting = excluded_airlines(text, store.airline_names(ctx))
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE kind = 'customer.excludes' AND lower(json_extract(payload, '$.customer')) = ?",
        (rfq.customer.lower(),),
    ).fetchall()
    earlier = {c for row in rows for c in json.loads(row["payload"])["carriers"]}
    if lasting:
        events.emit(ctx, "customer.excludes", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload={"customer": rfq.customer, "carriers": list(names)})  # fmt: skip
    return rfq.model_copy(update={"excluded_carriers": tuple(sorted(set(names) | earlier))})


# Where an older mail starts below the request: "-----Original Message-----", "On ... wrote:", a quoted line.
HISTORY = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|forwarded message|ursprüngliche nachricht|weitergeleitete nachricht)"
    r"|(?:from|von):.*@|on .{0,80} wrote:\s*$|am .{0,80} schrieb|>)",
    re.IGNORECASE | re.MULTILINE,
)
REPLY_SUBJECT = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*:\s*)+", re.IGNORECASE)


def own_words(body: str) -> str:
    """The mail without the older mails quoted below it: round 14 quoted an old request with its own lane, weight,
    greeting and name under the new one. Pricing and the reply both read only this."""
    cut = HISTORY.search(body)
    return body[: cut.start()] if cut and body[: cut.start()].strip() else body


def _body(message: Message) -> str:
    return own_words(message.body)


def _text(message: Message) -> str:
    """Subject and body; a reply's or forward's subject belongs to the older mail it answers."""
    subject = "" if REPLY_SUBJECT.match(message.subject) else message.subject
    return f"{subject}\n\n{_body(message)}"


def _largest(patterns: tuple[re.Pattern, ...], text: str, thousands: bool) -> Decimal | None:
    """The largest figure: the total beside per-piece and net figures."""
    values = [v for p in patterns for m in p.findall(text) if (v := number(m, thousands)) is not None and v > 0]
    return max(values, default=None)


def by_rules(message: Message, deadline: Deadline, guess: bool = False, *, cargo: dict | None = None) -> Rfq | None:
    """Regex and lookup tables. None unless origin, destination and weight are certain. With `guess`, for a model
    that cannot be reached: the largest gross weight and volume the mail names, rather than no quote at all."""
    text = _text(message)
    lane = _lane(text)
    door, postcode = _pickup(text)
    if lane is None and door and (destination := _destination_only(text)):
        lane = ("", destination)  # the origin is the pickup zone's airport, set in `extract`
    if cargo is None:
        lots = _lots(text)
        pieces = lots[2] if lots else _pieces(text)
        weight = lots[0] if lots else _weight(text, pieces)
        volume = lots[1] if lots else _volume(text, pieces)
    else:
        pieces, weight, volume = (cargo.get(k) for k in ("pieces", "weight_kg", "volume_cbm"))
    if guess:
        weight = weight or _largest(WEIGHT, text, thousands=True)
        volume = volume or _largest(VOLUME, text, thousands=False)
    elif volume is None and (DIMS.search(text) or any(p.search(text) for p in VOLUME)):
        return None  # a volume is given but not certain: under-reading it under-prices the chargeable weight
    if lane is None or weight is None:
        return None
    return Rfq(
        session_id=message.session_id,
        message_id=message.id,
        customer=message.sender,
        origin=lane[0],
        destination=lane[1],
        weight_kg=weight,
        volume_cbm=volume or Decimal(0),
        pieces=pieces,
        preference=_preference(text),
        door_pickup=door,
        pickup_postcode=postcode,
        pickup_zone=zone_of(postcode) if postcode else None,
        currency=_currency(text),
        language=_language(text),
        received_at=deadline.received_at,
        deadline_at=deadline.expires_at,
        missing=("pickup_postcode",) if door and not postcode else (),
    )


def _dims_volume(text: str) -> Decimal | None:
    dims = DIMS.findall(text)
    if len(dims) != 1:
        return None
    *sides, unit = dims[0]
    volume = Decimal(1)
    for side in sides:
        volume *= number(side, thousands=False) / _per_m(unit)
    return volume


def _written(text: str) -> set[Decimal]:
    """Every number the mail writes, read with and without thousands separators."""
    found = re.findall(NUM, text)
    return {v for m in found for t in (True, False) if (v := number(m, t)) is not None}


def _model_lots(raw: list[dict]) -> tuple[Decimal, Decimal, int | None] | None:
    """The piece groups as the model copied them, added up here; None unless every one has a weight and a volume."""
    weight, volume, pieces = Decimal(0), Decimal(0), 0
    for lot in raw:
        n = lot.get("pieces") if isinstance(lot.get("pieces"), int) and lot["pieces"] > 0 else None
        kg = number(str(lot.get("weight") or ""))
        cbm = number(str(lot.get("volume_cbm") or ""), thousands=False)
        one = _dims_volume(str(lot.get("dimensions") or ""))
        if kg is None or kg <= 0 or (cbm is None and one is None):
            return None
        if (lot.get("weight_per_piece") or cbm is None or lot.get("volume_per_piece")) and n is None:
            return None
        weight += kg * (LB if lot.get("weight_unit") == "lb" else 1) * (n if lot.get("weight_per_piece") else 1)
        volume += one * n if cbm is None else cbm * (n if lot.get("volume_per_piece") else 1)
        pieces = pieces + n if pieces is not None and n else None
    return (weight, volume, pieces) if raw else None


def _from_model(data: dict, message: Message, deadline: Deadline) -> Rfq:
    text = _text(message)
    missing = []

    def code(key: str) -> str:
        value = (data.get(key) or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            missing.append(key)
            return ""
        return value

    origin, destination = code("origin"), code("destination")
    weight = number(data.get("weight_kg") or "")
    if weight is None or weight <= 0:
        weight = Decimal(0)
    pieces = data.get("pieces")
    volume = number(data.get("volume_cbm") or "", thousands=False)
    if volume is None or volume < 0:
        volume = _volume(text, pieces) or Decimal(0)
    # The sums of a lot list are arithmetic: code's, not the model's (its own total was 1094 kg for 1952 kg of lots).
    # One copied group counts only without a total in kg: a weight in pounds.
    groups = data.get("lots") or []
    if lots := _lots(text) or ((len(groups) > 1 or not weight) and _model_lots(groups)):
        weight, volume, pieces = lots[0], lots[1], lots[2] or pieces
    else:
        # A figure the mail does not write is the model's arithmetic ("480 lbs" to kg, dimensions to cbm): code's
        # own reading of the parts replaces it.
        written = _written(text)
        if (not weight or weight not in written) and (
            own := _weight(text, pieces) or (_model_lots(groups) or (None,))[0]
        ):
            weight = own
        if volume not in written and (own := _volume(text, pieces) or (_model_lots(groups) or (0, None))[1]):
            volume = own
    if not weight:
        missing.append("weight_kg")
    door = bool(data.get("door_pickup"))
    postcode = re.sub(r"\D", "", data.get("pickup_postcode") or "")
    postcode = postcode if len(postcode) == 5 else None
    if door and not postcode:
        missing.append("pickup_postcode")
    currency = (data.get("currency") or "").strip().upper() or None
    return Rfq(
        session_id=message.session_id,
        message_id=message.id,
        customer=message.sender,
        origin=origin,
        destination=destination,
        weight_kg=weight,
        volume_cbm=volume,
        pieces=pieces if isinstance(pieces, int) and pieces > 0 else None,
        preference=Preference.FASTEST if data.get("preference") == "fastest" else Preference.CHEAPEST,
        door_pickup=door,
        pickup_postcode=postcode,
        pickup_zone=zone_of(postcode) if postcode else None,
        currency=currency if currency and re.fullmatch(r"[A-Z]{3}", currency) else None,
        language=_language(text),  # the model answered "de" for an English mail with German place names
        received_at=deadline.received_at,
        deadline_at=deadline.expires_at,
        extracted_by="model",
        missing=tuple(missing),
    )


def _plain(text: str) -> str:
    return text.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))


PLACE_WORDS = re.compile(r"\b(?:site|warehouse|premises|plant|factory|lager|werk|standort)\b", re.IGNORECASE)


def _known_sites(ctx: Context, rfq: Rfq) -> list[str]:
    """The pickup postcodes this customer gave in earlier requests, first named first (the CRM's memory)."""
    rows = ctx.db.execute(
        "SELECT json_extract(payload, '$.pickup_postcode') AS postcode FROM events WHERE kind = 'rfq.extracted'"
        " AND json_extract(payload, '$.customer') = ? AND session_id != ? AND postcode IS NOT NULL ORDER BY id",
        (rfq.customer, rfq.session_id),
    ).fetchall()
    return list(dict.fromkeys(r["postcode"] for r in rows))


def _town_zone(ctx: Context, rfq: Rfq, text: str, guess: bool = True) -> Rfq:
    """A door-to-port pickup without a postcode ("pickup from our warehouse"): the town named where the pickup is
    described. Without it the place stays open to be asked; only with `guess` (no answer in time) the customer's
    first-named earlier site, then a town anywhere in the mail such as the signature's. The history was right once
    (round 19) and wrong twice: asked, Lindqvist named 51115 (not 21663), Weidner Augsburg (not 61391)."""
    if not rfq.door_pickup or rfq.pickup_zone:
        return rfq
    areas = {_plain(r["area"]): r["zone"] for r in ctx.db.execute("SELECT zone, area FROM pickup_zones")}

    def towns(part: str) -> set[str]:
        named = {a for a in areas if re.search(rf"\b{re.escape(a)}\b", _plain(part))}
        return {areas[a] for a in named if not any(a != b and a in b for b in named)}  # "Munich" in "Munich South"

    zones = towns("\n".join(line for line in text.splitlines() if DOOR.search(line) or PLACE_WORDS.search(line)))
    postcode, sites = None, _known_sites(ctx, rfq)
    if len(zones) != 1 and sites and guess:
        postcode = sites[0]
        zones = {zone_of(postcode)}
    if len(zones) != 1 and guess:
        zones = towns(text)
    if len(zones) != 1:
        return rfq
    return rfq.model_copy(update={"pickup_zone": zones.pop(), "pickup_postcode": postcode,
                                  "missing": tuple(m for m in rfq.missing if m != "pickup_postcode")})  # fmt: skip


ASK_WAIT_S = 60  # customers answered within about 10 s, truckers about 31 s later: the quote stays inside 120 s
FACTS = {
    "pickup": ("the postcode we should collect the goods from", "die Postleitzahl, unter der wir die Ware abholen"),
    "origin": ("the airport the goods leave from", "den Abflughafen"),
    "destination": ("the destination airport", "den Zielflughafen"),
    "weight": ("the gross weight in kg", "das Bruttogewicht in kg"),
    "volume": ("the volume or the dimensions of the pieces", "das Volumen oder die Maße der Packstücke"),
}
ASK = {
    "en": "Hello,\n\nthank you for your request. Could you tell us {facts}? As soon as we have it, you will receive "
    "our all-in offer.\n\nBest regards\n{signer}",
    "de": "Guten Tag,\n\nvielen Dank für Ihre Anfrage. Könnten Sie uns noch {facts} nennen? Sobald wir das haben, "
    "erhalten Sie unser Angebot.\n\nFreundliche Grüße\n{signer}",
}


def _unknown(rfq: Rfq) -> list[str]:
    """The facts a price still needs that the request did not give."""
    facts = ["pickup"] if rfq.door_pickup and not rfq.pickup_zone else ["origin"] if not rfq.origin else []
    # Without a volume the chargeable weight is the gross weight, too low for bulky goods.
    missing = ["destination"] * (not rfq.destination) + ["weight"] * (rfq.weight_kg <= 0)
    return facts + missing + ["volume"] * (rfq.volume_cbm <= 0)


async def _ask(ctx: Context, message: Message, rfq: Rfq, facts: list[str], deadline: Deadline) -> str | None:
    """One short mail in the customer's thread asking for exactly the missing facts, never a price; their answer if
    it comes within ASK_WAIT_S of the request. A customer held back the pickup and gave it at once when asked (round
    18). Asked once per request."""
    if ctx is None:
        return None
    if ctx.db.execute("SELECT 1 FROM messages WHERE session_id = ? AND purpose = 'question'",
                      (message.session_id,)).fetchone():  # fmt: skip
        return None
    de = rfq.language == "de"
    names = [FACTS[f][de] for f in facts]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {'und' if de else 'and'} {names[-1]}"
    subject = message.subject if REPLY_SUBJECT.match(message.subject) else f"Re: {message.subject}"
    try:
        question = await outbox.send(
            ctx, to=message.sender, subject=subject, session_id=message.session_id, in_reply_to=message.id,
            body=ASK["de" if de else "en"].format(facts=joined, signer=ctx.settings.signer), purpose="question",
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - held (dry run, takeover) or failed: the guess takes over
        return None
    # Not the request itself: a replay's own mail ids are negative.
    answer = ("SELECT body FROM messages WHERE session_id = ? AND direction = 'in' AND id > ? AND id != ?"
              " AND lower(sender) = ? ORDER BY id LIMIT 1")  # fmt: skip
    asked = (message.session_id, question.id, message.id, message.sender.lower())
    while not (row := ctx.db.execute(answer, asked).fetchone()):
        if timing.now() >= deadline.received_at + timedelta(seconds=ASK_WAIT_S):
            return None
        await asyncio.sleep(1)
    return row["body"]


def _zone_origin(ctx: Context, rfq: Rfq, text: str) -> Rfq:
    """The pickup zone belongs to one airport (R16): a door-to-port request leaves from it unless the mail itself
    names its origin airport. A model guesses from the town: XFW for Lueneburg (zone HAM), CGN for Wuppertal (zone
    DUS); no airline flew those lanes and both requests were missed."""
    if not rfq.pickup_zone or not (airport := store.airport_of(ctx, rfq.pickup_zone)):
        return rfq
    if rfq.origin and (not rfq.door_pickup or names_code(text, rfq.origin)):
        return rfq
    return rfq.model_copy(update={"origin": airport, "missing": tuple(m for m in rfq.missing if m != "origin")})


def _declared(lines: list[str]) -> str | None:
    found = (m for line in lines if (m := DANGEROUS.search(line)))
    return next((" ".join((m[1] or m[2]).split()) for m in found), None)


async def _attachments(ctx: Context, message: Message) -> list[tuple[str, list[str]]]:
    """The text lines of every attachment code can read (packing list, letter of instruction)."""
    from pixel.agents.tariffs.agent import _lines

    files = []
    for attachment in message.attachments:
        try:
            files.append((attachment.filename, _lines(attachment.filename,
                                                      await ctx.mailbox.attachment(message.id, attachment.id))))  # fmt: skip
        except Exception as e:  # noqa: BLE001 - an unreadable attachment must not cost the quote
            events.emit(ctx, "error", "rfq", session_id=message.session_id, message_id=message.id,
                        payload={"where": "rfq_attachment", "file": attachment.filename, "error": type(e).__name__})  # fmt: skip
    return files


def names_code(text: str, code: str) -> bool:
    return bool(code and re.search(rf"\b{re.escape(code)}\b", text, re.IGNORECASE))


COLUMN = re.compile(
    r"(?P<name>width|length|height|breite|l(?:ä|ae)nge|h(?:ö|oe)he|pieces|pcs|qty|quantity|st(?:ü|ue)ck|"
    r"gross\s+weight|weight|gewicht)(?:\s*\((?P<unit>mm|cm|m|kgs?)\))?",
    re.IGNORECASE,
)
SIDE = {"width": 0, "breite": 0, "length": 1, "länge": 1, "laenge": 1, "height": 2, "höhe": 2, "hoehe": 2}
COUNT = {"pieces", "pcs", "qty", "quantity", "stück", "stueck"}


def _table(text: str) -> tuple[list[tuple[str, str | None]], list[list[str]], str] | None:
    """A packing table: its columns (name, unit) from a header naming width, length, height and pieces, the rows
    below it with one number per column, and the text around it."""
    lines = text.splitlines()
    for i, header in enumerate(lines):
        columns = [(m["name"].casefold(), m["unit"]) for m in COLUMN.finditer(header)]
        names = [name for name, _ in columns]
        if not ({0, 1, 2} <= {SIDE.get(n) for n in names} and COUNT & set(names)):
            continue
        rows = []
        for row in lines[i + 1 :]:
            cells = re.findall(r"\d[\d.,]*", row)
            if len(cells) != len(columns):
                break
            rows.append(cells)
        return columns, rows, "\n".join(lines[:i] + lines[i + 1 + len(rows) :])
    return None


def _table_volume(text: str) -> Decimal | None:
    """The volume a packing table's rows add up to."""
    columns, rows, _ = _table(text) or ([], [], "")
    total = None
    for row in rows:
        cells = [number(c, thousands=False) for c in row]
        if None in cells:
            break
        volume = Decimal(1)
        for (name, unit), cell in zip(columns, cells, strict=True):
            if name in SIDE:
                volume *= cell / _per_m(unit)
            elif name in COUNT:
                volume *= cell
        total = (total or Decimal(0)) + volume
    return total


def _document_weight(text: str) -> Decimal | None:
    """A document's stated total gross weight when its table's one weight column, headed in kg, adds up to it
    (round 35: the mail said 2644 kg, the spec's total and its row 1751 kg). A row's weight is never multiplied by
    its pieces, and a weight per piece or a second weight column is ambiguous: then the mail stands."""
    if (table := _table(text)) is None or EACH_WEIGHT.search(text):
        return None
    # Read the total off the table's own lines: "Gross weight (kg)" ending a header runs on into the first row.
    columns, rows, around = table
    if (stated := _unique(WEIGHT, around, thousands=True)) is None:
        return None
    weights = [i for i, (name, _) in enumerate(columns) if "weight" in name or name == "gewicht"]
    if len(weights) != 1 or columns[weights[0]][1] not in ("kg", "kgs") or not rows:
        return None
    cells = [number(row[weights[0]]) for row in rows]
    if None in cells or abs(sum(cells) - stated) > Decimal("0.5"):
        return None
    return stated


def _document_volume(text: str, pieces: int | None) -> Decimal | None:
    """A document's stated total volume when its own table or dimensions bear it out (round 33)."""
    stated = _unique(VOLUME, text, thousands=False)
    if stated is None or EACH_VOLUME.search(text):
        return None
    one = _dims_volume(text)
    measured = _table_volume(text) or (one * pieces if one is not None and pieces else None)
    if measured is None or abs(measured - stated) > max(Decimal("0.01"), stated * Decimal("0.01")):
        return None
    return stated


DOCUMENT_AIRPORT = re.compile(
    r"(?m)^[ \t]*(?P<label>(?i:origin|departure|destination|abgangsflughafen|zielflughafen))"
    r"(?i:[ \t]+airport)?[ \t]*:[ \t]*(?P<code>[A-Z]{3})\b"
)
CARGO_CLAUSE = re.compile(r"[\n;]|(?<=[.!?])\s+")
USE_CARGO = re.compile(r"^(?:please\s+)?(?:use|quote\s+(?:with|using))\s+(.+)", re.IGNORECASE)
MAIL_CARGO = re.compile(r"^(.+?)\s+(?:in|from)\s+(?:this|my|our|the)\s+(?:e-?mail|message|body)\b", re.IGNORECASE)
# Also require a correction marker before treating a plain "weight is now ..." as an instruction.
PREFIXED_MEASUREMENT = re.compile(
    r"^(?:correction|korrektur)\s*:\s*(?:the\s+|das\s+|die\s+)?"
    r"(?P<field>(?:gross\s+)?weight|volume|dimensions|bruttogewicht|gewicht|volumen|maße)"
    r"\s*(?:is\b|are\b|beträgt\b|betragen\b|ist\b|sind\b|:)\s*(?:(?:now|jetzt|nun)\s+)?(?P<value>.+)",
    re.IGNORECASE,
)
NEW_MEASUREMENT = re.compile(
    r"^(?:the\s+)?(?:corrected|revised|updated)\s+(?P<field>(?:gross\s+)?weight|volume|dimensions)"
    r"\s*(?:is\b|are\b|:)\s*(?P<value>.+)",
    re.IGNORECASE,
)
ORIGIN_FIELDS = {"origin", "departure", "abgangsflughafen"}
CARGO_FIELDS = {
    "weight_kg": re.compile(r"\b(?:weight|measurements|figures|bruttogewicht|gewicht)\b", re.IGNORECASE),
    "volume_cbm": re.compile(r"\b(?:volume|dimensions|measurements|figures|volumen|maße)\b", re.IGNORECASE),
}


def _shipment_sources(sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Exclude attachments that explicitly name a different airport for this shipment."""
    if len(sources) == 1:
        return sources
    mail = sources[0][1]
    origin, destination = _lane(mail) or (None, _destination_only(mail))
    kept = sources[:1]
    for name, text in sources[1:]:
        airports = (
            (m["code"], origin if m["label"].lower() in ORIGIN_FIELDS else destination)
            for m in DOCUMENT_AIRPORT.finditer(text)
            if _airport(m["code"])
        )
        if not any(wanted and listed != wanted for listed, wanted in airports):
            kept.append((name, text))
    return kept


def _cargo_sources(sources: list[tuple[str, str]]) -> tuple[dict, dict, Decimal | None]:
    """Use explicit corrections, then corroborated measurements from the eligible attachments."""
    clauses = [part.strip() for part in CARGO_CLAUSE.split(sources[0][1])]
    directives = [
        re.split(r"\b(?:not|instead of|rather than)\b", match[1], maxsplit=1, flags=re.IGNORECASE)[0]
        for clause in clauses
        if (match := USE_CARGO.match(clause))
    ]
    mail_fields = [match[1] for directive in directives if (match := MAIL_CARGO.match(directive))]
    corrections = [
        match for clause in clauses if (match := NEW_MEASUREMENT.match(clause) or PREFIXED_MEASUREMENT.match(clause))
    ]
    named = [
        (name, text)
        for name, text in sources[1:]
        if any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-]|\.\w)", item, re.IGNORECASE) for item in directives)
    ]
    if len(named) == 1:
        sources = [sources[0], *named]
    lots = [_lots(text) for _, text in sources]
    facts, evidence = {}, {}
    document_weight = None

    def take(field: str, readings: list) -> None:
        if readings[0] is not None:
            facts[field], evidence[field] = readings[0], [sources[0][0]]
        elif len(values := {v for v in readings if v is not None}) == 1:
            facts[field] = values.pop()
            evidence[field] = [name for (name, _), v in zip(sources, readings, strict=True) if v is not None]

    counts = [group[2] if group else _pieces(text) for (_, text), group in zip(sources, lots, strict=True)]
    take("pieces", counts)
    for field, index, reader in (("weight_kg", 0, _weight), ("volume_cbm", 1, _volume)):
        field_pattern = CARGO_FIELDS[field]
        corrected = {
            value
            for match in corrections
            if field_pattern.search(match["field"])
            and (value := reader(match["value"], facts.get("pieces"))) is not None
        }
        if len(corrected) == 1:
            facts[field], evidence[field] = corrected.pop(), [sources[0][0]]
            continue
        readings = [
            group[index] if group else reader(text, count or facts.get("pieces"))
            for (_, text), group, count in zip(sources, lots, counts, strict=True)
        ]
        take(field, readings)
        if readings[0] is not None and any(field_pattern.search(fields) for fields in mail_fields):
            continue
        if field == "weight_kg":
            documents = [(name, _document_weight(text)) for name, text in sources[1:]]
        else:
            documents = [(name, _document_volume(text, facts.get("pieces"))) for name, text in sources[1:]]
        if len(corroborated := {v for _, v in documents if v is not None}) == 1:
            facts[field] = corroborated.pop()
            evidence[field] = [name for name, v in documents if v is not None]
            if field == "weight_kg":
                document_weight = facts[field]
    return facts, evidence, document_weight


def _document_pickup(rfq: Rfq, sources: list[tuple[str, str]], evidence: dict) -> Rfq:
    """Use the pickup address in the instructions when the customer's mail supplies no address."""
    if OWN_DELIVERY.search(sources[0][1]):
        return rfq
    addresses = [(name, _pickup(text)) for name, text in sources]
    door = rfq.door_pickup or any(wanted for _, (wanted, _) in addresses)
    if not door:
        return rfq
    postcode = rfq.pickup_postcode
    if not postcode:
        given = {code for _, (_, code) in addresses if code}
        if len(given) == 1:
            postcode = given.pop()
    if postcode:
        evidence["pickup_postcode"] = [name for name, (_, code) in addresses if code == postcode]
        return rfq.model_copy(update={"door_pickup": True, "pickup_postcode": postcode, "pickup_zone": zone_of(postcode),
                                      "missing": tuple(k for k in rfq.missing if k != "pickup_postcode")})  # fmt: skip
    missing = tuple(dict.fromkeys((*rfq.missing, "pickup_postcode")))
    return rfq.model_copy(update={"door_pickup": True, "missing": missing})


async def dangerous_goods(
    ctx: Context, message: Message, files: list[tuple[str, list[str]]] | None = None
) -> tuple[str, str] | None:
    """What the mail or one of its attachments (packing list, letter of instruction) declares as dangerous goods,
    and where: also when the mail itself says "non-dangerous"."""
    if declared := _declared(_body(message).splitlines()):
        return declared, "mail"
    for filename, lines in files if files is not None else await _attachments(ctx, message):
        if declared := _declared(lines):
            return declared, filename
    return None


async def extract(ctx: Context, message: Message, deadline: Deadline) -> Rfq:
    rfq, _ = await read_request(ctx, message, deadline)
    return rfq


async def read_request(ctx: Context, message: Message, deadline: Deadline) -> tuple[Rfq, str]:
    """by_rules, else one `llm.ask_json` call that also drafts the quote's prose slots (`prose.raw`, R31), so the
    quote still costs one call. Emits `rfq.extracted` with `extracted_by`."""
    files = await _attachments(ctx, message) if message.attachments else []
    mail = message
    documents = [(name, own_words("\n".join(lines))) for name, lines in files]

    def source_inputs() -> tuple[list[tuple[str, str]], str, Message]:
        selected = _shipment_sources([("mail", _text(mail)), *documents])
        combined = "\n\n".join(content for _, content in selected)
        combined_mail = mail.model_copy(update={"subject": "", "body": combined}) if files else mail
        return selected, combined, combined_mail

    sources, text, message = source_inputs()
    cargo, evidence, document_weight = _cargo_sources(sources) if files else ({}, {}, None)

    def rules(guess: bool = False) -> Rfq | None:
        if not files:
            return by_rules(mail, deadline, guess=True) if guess else by_rules(mail, deadline)
        # Prefer the customer's routing and instructions; consult the documents if those are missing.
        has_unread_volume = "volume_cbm" not in cargo and (DIMS.search(text) or any(p.search(text) for p in VOLUME))
        if has_unread_volume and not guess:
            return None
        return by_rules(mail, deadline, guess, cargo=cargo) or by_rules(message, deadline, guess, cargo=cargo)

    def apply_pickup(read: Rfq, guess: bool = True) -> Rfq:
        if files:
            read = _document_pickup(read, sources, evidence)
        return _zone_origin(ctx, _town_zone(ctx, read, text, guess), text)

    async def ask(read: Rfq) -> Rfq:
        """Asks once for the facts still missing and reads the request again with the customer's answer."""
        nonlocal mail, message, sources, text, cargo, evidence, document_weight
        read = apply_pickup(read, guess=False)
        if not (facts := _unknown(read)) or (said := await _ask(ctx, mail, read, facts, deadline)) is None:
            return read
        mail = mail.model_copy(update={"body": f"{_body(mail)}\n\n{own_words(said)}"})
        sources, text, message = source_inputs()
        cargo, evidence, document_weight = _cargo_sources(sources) if files else ({}, {}, None)
        again = rules()
        if (
            again is None
            and read.door_pickup
            and not read.pickup_zone
            and len(codes := set(POSTCODE.findall(own_words(said)))) == 1
        ):
            # The model read the request, so the rules cannot read it again: the place comes from the answer alone.
            code = codes.pop()
            again = read.model_copy(update={"pickup_postcode": code, "pickup_zone": zone_of(code),
                                            "missing": tuple(m for m in read.missing if m != "pickup_postcode")})  # fmt: skip
        return apply_pickup(again or read, guess=False)

    rfq, prose = rules(), None
    if rfq is not None and not (rfq := apply_pickup(await ask(rfq))).origin:
        rfq = None
    if rfq is None:
        try:
            result = await llm.ask_json(
                ctx,
                purpose="rfq.extract",
                system=SYSTEM,
                user=text,
                schema=SCHEMA,
                session_id=message.session_id,
                max_output_tokens=500,
            )
        except Exception as e:
            # A gateway that refuses the model must not cost the request: without an Rfq nothing can be quoted.
            guessed = rules(guess=True)
            if guessed is None or not (guessed := apply_pickup(guessed)).origin:
                raise
            events.emit(
                ctx,
                "error",
                "rfq",
                session_id=message.session_id,
                message_id=message.id,
                payload={
                    "where": "rfq_extract_model",
                    "error": type(e).__name__,
                    "status": getattr(e, "status", None),
                    "default": "guess",
                },
            )  # fmt: skip  never the gateway's text: it can quote the OpenAI key
            rfq = guessed.model_copy(update={"extracted_by": "guess"})
        else:
            rfq = _from_model(result.data, message, deadline)
            if cargo:
                rfq = rfq.model_copy(update={**cargo, "missing": tuple(k for k in rfq.missing if k not in cargo)})
            rfq = apply_pickup(await ask(rfq))
            prose = {k: result.data.get(k) for k in PROSE}
    if files:
        rfq = rfq.model_copy(update={"language": _language(_text(mail))})
        for key in ("origin", "destination"):
            if named := [name for name, content in sources if names_code(content, getattr(rfq, key))]:
                evidence[key] = named
        excluded = [name for name, content in documents if (name, content) not in sources]
        proof = {"document_weight_kg": document_weight} if document_weight == rfq.weight_kg else {}
        events.emit(ctx, "rfq.evidence", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload={"sources": evidence, "attachment_route_conflicts": excluded, **proof})  # fmt: skip
    rfq = _ruled_out(ctx, rfq, _text(mail))
    if dg := await dangerous_goods(ctx, mail, files):  # the mail as written: the note says where it stood
        # The quote names the goods and asks for the shipper's declaration (DGD) and the safety data sheet (MSDS).
        rfq = rfq.model_copy(update={"missing": (*rfq.missing, DG_DOCUMENTS)})
        events.emit(ctx, "rfq.dangerous_goods", "rfq", session_id=message.session_id, message_id=message.id,
                    payload={"declared": dg[0], "source": dg[1]})  # fmt: skip
    events.emit(
        ctx,
        "rfq.extracted",
        "rfq",
        session_id=message.session_id,
        message_id=message.id,
        payload=rfq.model_dump(mode="json"),
    )
    if prose is not None:
        events.emit(ctx, "prose.raw", "rfq", session_id=message.session_id, message_id=message.id, payload=prose)
    return rfq, text
