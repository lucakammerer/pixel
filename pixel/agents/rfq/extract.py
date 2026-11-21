import re
from decimal import Decimal, InvalidOperation

from pixel import llm
from pixel.contracts import Message, Preference, Rfq
from pixel.core import events
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
ORIGIN_LABEL = re.compile(rf"(?i:\b(?:origin|from|von|ab|departure|abgangsflughafen)\b)\s*:?[^\n:]{{0,30}}?{CODE}")
DEST_LABEL = re.compile(rf"(?i:\b(?:destination|to|nach|bis|zielflughafen)\b)\s*:?[^\n:]{{0,30}}?{CODE}")

NUM = r"(\d[\d.,']*)"
WEIGHT = (
    re.compile(rf"{NUM}\s*(?i:kgs?|kilos?|kilograms?|kilogramm)\b"),
    re.compile(rf"(?i:\b(?:gross weight|weight|gewicht|bruttogewicht))\s*:?\s*{NUM}"),
)
VOLUME = (
    re.compile(rf"{NUM}\s*(?:(?i:cbm|cubic met\w*|kubikmeter)\b|m3\b|m³)"),
    re.compile(rf"(?i:\b(?:cbm|volume|volumen))\s*:?\s*{NUM}"),
)
DIMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m)?\b", re.IGNORECASE
)
LOT = re.compile(r"(?im)^\s*(?:lot|position|pos\.?|item|los)\s*\d+\s*[:.)-]\s*(\d+)\s+[^\W\d](.+)$")
EACH = r"each|apiece|per\s+(?:piece|unit|carton|case|drum|pallet|box|crate)|je|pro\s+st(?:ü|ue)ck"
ALL = r"together|in\s+total|total|combined|altogether|zusammen|gesamt|insgesamt"
MODE = rf"(?:\b(?P<each>{EACH})|\b(?P<all>{ALL}))?\s*"
LOT_WEIGHT = re.compile(rf"{MODE}{NUM}\s*(?:kgs?|kilos?)\b(?:\s*(?P<kind>gross|brutto|net|netto))?", re.IGNORECASE)
LOT_VOLUME = re.compile(rf"{MODE}{NUM}\s*(?:cbm\b|m3\b|m³)", re.IGNORECASE)
LOT_DIMS = re.compile(MODE + DIMS.pattern, re.IGNORECASE)
PIECES = re.compile(
    r"(\d+)\s*(?:pcs|pieces|pallets?|colli|packages|cartons|boxes|stück|stk|paletten|kartons)\b", re.IGNORECASE
)
FASTEST = re.compile(
    r"\b(?:fastest|quickest|urgent\w*|asap|express|as soon as possible|schnellst\w*|eilig\w*|dringend\w*)\b",
    re.IGNORECASE,
)
DOOR = re.compile(
    r"door[\s-]*to[\s-]*(?:port|airport)|pick[\s-]?up|collection from|collect from|abhol\w*|ab werk|ab haus",
    re.IGNORECASE,
)
POSTCODE = re.compile(r"(?<![\d.,])(?:D-)?(\d{5})(?![\d.,]?\d)(?!\s*(?:kg|kgs|cbm|m3|m³|pcs)\b)", re.IGNORECASE)
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
    "code of the mail. Never calculate or guess. "
    "Also write three short pieces for our reply, in the mail's language and tone (German: formal Sie): first_name, "
    "the customer's first name only if they signed with it, else null; intro, one or two warm sentences that show "
    "you read their mail (the goods, urgency, timing or a question they asked, in words); closing, one short "
    "friendly last line that is not a call to action, or null. These three never contain digits, prices, dates, "
    "airlines, airports or places, and never greet or sign."
)
NULLABLE_STR = {"type": ["string", "null"]}
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
        "preference": {"type": "string", "enum": ["cheapest", "fastest"]},
        "door_pickup": {"type": "boolean"},
        "pickup_postcode": NULLABLE_STR,
        "currency": NULLABLE_STR,
        "language": {"type": "string"},
        "first_name": NULLABLE_STR,
        "intro": NULLABLE_STR,
        "closing": NULLABLE_STR,
    },
    "required": ["origin", "destination", "weight_kg", "volume_cbm", "pieces", "preference", "door_pickup",
                 "pickup_postcode", "currency", "language", "first_name", "intro", "closing"],
}  # fmt: skip


def number(text: str, thousands: bool = True) -> Decimal | None:
    s = text.strip().replace("'", "").replace(" ", "").rstrip(".,")
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


def _unique(patterns: tuple[re.Pattern, ...], text: str, thousands: bool) -> Decimal | None:
    values = {v for p in patterns for m in p.findall(text) if (v := number(m, thousands)) is not None and v > 0}
    return values.pop() if len(values) == 1 else None


def _pieces(text: str) -> int | None:
    found = {int(m) for m in PIECES.findall(text)}
    return found.pop() if len(found) == 1 else None


def _volume(text: str, pieces: int | None) -> Decimal | None:
    stated = _unique(VOLUME, text, thousands=False)
    if stated is not None:
        return stated
    dims = DIMS.findall(text)
    if len(dims) != 1:
        return None
    *sides, unit = dims[0]
    per_m = {"mm": Decimal(1000), "m": Decimal(1)}.get(unit.lower(), Decimal(100))
    volume = Decimal(1)
    for side in sides:
        volume *= number(side, thousands=False) / per_m
    return volume * (pieces or 1)


def _lots(text: str) -> tuple[Decimal, Decimal, int] | None:
    """Gross weight, volume and pieces of a request that lists its shipment lot by lot ("Lot 2: 4 cartons, each
    48 kg gross, each 110 x 110 x 52 cm"). Each lot states its figures per piece or for the lot; code adds them
    up. None unless there are several lots and every one is certain."""
    lots = LOT.findall(text)
    weight, volume, pieces = Decimal(0), Decimal(0), 0
    for count, rest in lots:
        n = int(count)
        weights = [m for m in LOT_WEIGHT.finditer(rest) if (m["kind"] or "").lower() not in ("net", "netto")]
        stated, dims = LOT_VOLUME.search(rest), LOT_DIMS.search(rest)
        if len(weights) != 1 or not (stated or dims) or not (weights[0]["each"] or weights[0]["all"] or n == 1):
            return None
        kg = number(weights[0].group(3))
        if stated:
            if not (stated["each"] or stated["all"] or n == 1):
                return None
            cbm = number(stated.group(3), thousands=False) * (n if stated["each"] else 1)
        else:
            *sides, unit = dims.groups()[2:]
            per_m = {"mm": Decimal(1000), "m": Decimal(1)}.get((unit or "cm").lower(), Decimal(100))
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
    if not DOOR.search(text):
        return False, None
    codes = set(POSTCODE.findall(text))
    return True, codes.pop() if len(codes) == 1 else None


def _currency(text: str) -> str | None:
    asked = {c for c in CURRENCY_ASK.findall(text) if c in CURRENCIES}
    return asked.pop() if len(asked) == 1 else None


def _language(text: str) -> str:
    words = re.findall(r"[a-zäöüß]+", text.lower())
    return "de" if sum(w in GERMAN for w in words) > sum(w in ENGLISH for w in words) else "en"


def _text(message: Message) -> str:
    return f"{message.subject}\n\n{message.body}"


def by_rules(message: Message, deadline: Deadline) -> Rfq | None:
    """Regex and lookup tables. None unless origin, destination and weight are certain."""
    text = _text(message)
    lane = _lane(text)
    lots = _lots(text)
    weight = lots[0] if lots else _unique(WEIGHT, text, thousands=True)
    if lane is None or weight is None:
        return None
    pieces = lots[2] if lots else _pieces(text)
    door, postcode = _pickup(text)
    return Rfq(
        session_id=message.session_id,
        message_id=message.id,
        customer=message.sender,
        origin=lane[0],
        destination=lane[1],
        weight_kg=weight,
        volume_cbm=lots[1] if lots else _volume(text, pieces) or Decimal(0),
        pieces=pieces,
        preference=Preference.FASTEST if FASTEST.search(text) else Preference.CHEAPEST,
        door_pickup=door,
        pickup_postcode=postcode,
        pickup_zone=zone_of(postcode) if postcode else None,
        currency=_currency(text),
        language=_language(text),
        received_at=deadline.received_at,
        deadline_at=deadline.expires_at,
        missing=("pickup_postcode",) if door and not postcode else (),
    )


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
        missing.append("weight_kg")
        weight = Decimal(0)
    pieces = data.get("pieces")
    volume = number(data.get("volume_cbm") or "", thousands=False)
    if volume is None or volume < 0:
        volume = _volume(text, pieces) or Decimal(0)
    if lots := _lots(text):  # the sums of a lot list are arithmetic: code's, not the model's
        weight, volume, pieces = lots
        missing = [m for m in missing if m != "weight_kg"]
    door = bool(data.get("door_pickup"))
    postcode = re.sub(r"\D", "", data.get("pickup_postcode") or "")
    postcode = postcode if len(postcode) == 5 else None
    if door and not postcode:
        missing.append("pickup_postcode")
    currency = (data.get("currency") or "").strip().upper() or None
    language = (data.get("language") or "").strip().lower()[:2]
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
        language=language if re.fullmatch(r"[a-z]{2}", language) else _language(text),
        received_at=deadline.received_at,
        deadline_at=deadline.expires_at,
        extracted_by="model",
        missing=tuple(missing),
    )


async def extract(ctx: Context, message: Message, deadline: Deadline) -> Rfq:
    """by_rules, else one `llm.ask_json` call that also drafts the quote's prose slots (`prose.raw`, R31), so the
    quote still costs one call. Emits `rfq.extracted` with `extracted_by`."""
    rfq, prose = by_rules(message, deadline), None
    if rfq is None:
        result = await llm.ask_json(
            ctx,
            purpose="rfq.extract",
            system=SYSTEM,
            user=_text(message),
            schema=SCHEMA,
            session_id=message.session_id,
            max_output_tokens=350,
        )
        rfq = _from_model(result.data, message, deadline)
        prose = {k: result.data.get(k) for k in PROSE}
    # A door-to-port request can name only the pickup address: the origin is then the zone's airport (R16).
    if not rfq.origin and rfq.pickup_zone and (airport := store.airport_of(ctx, rfq.pickup_zone)):
        rfq = rfq.model_copy(update={"origin": airport, "missing": tuple(m for m in rfq.missing if m != "origin")})
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
    return rfq
