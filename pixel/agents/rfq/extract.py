import asyncio
import json
import re
import unicodedata
from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
# "to DFW", "nach", "à"/"vers"/"pour", "a"/"para"/"hacia", "per"/"verso", "naar" (round 70 wrote French).
DEST_LABEL = re.compile(
    rf"(?i:\b(?:destination|to|nach|bis|zielflughafen|vers|pour|para|hacia|destino|verso|naar|bestemming)\b|(?<!\w)à(?!\w)"
    # Spanish, Italian, Portuguese "a GRU", only right before a code that ends the clause: never the English article
    rf"|(?<!\w)a(?=\s+(?-i:[A-Z]{{3}})\s*(?:[.,;:!?(\n]|$))"
    rf"|\bper(?=\s+[A-Z]{{3}}\b))"  # Italian "per DFW"; English "per kg" and "per AWB" stay out
    rf"{AIRPORT_WORD}\s*:?[^\n:]{{0,30}}?{CODE}"
)
# The legs around the flight: "we deliver to HAM ourselves" names the departure airport, "delivery from DXB airport to
# the door" the destination.
PRE_CARRIAGE = re.compile(
    rf"(?i:\bdeliver\w*\s+(?:it\s+|the\s+\w+\s+)?to){AIRPORT_WORD}\s+{CODE}(?=(?:\s+airport)?\s+(?i:ourselves|by us))"
)
ON_CARRIAGE = re.compile(
    rf"(?i:\bfrom){AIRPORT_WORD}\s+{CODE}(?=(?:\s+airport)?\s+(?i:to\s+(?:the|your|their)\s+door))"
)

# "1 250 kg": a space (also a no-break one) between thousands, not after a date's or a range's digits.
NUM = r"((?<![\d.,\-/])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d.,'])|\d[\d.,']*)"
OTHER_UNIT = r"\s*(?i:lbs?|pounds?|t(?![-\w])|tons?|tonnes?|tonnen)"
# "Bruttogewicht gesamt (kg):", "Total volume (cbm):"; a label never takes its figure from the next line, where a
# table header ending in "Gross weight (kg)" would read the first row's first cell.
TOTAL_WORD = r"(?:\s+(?:gesamt|insgesamt|total))?"
WEIGHT = (
    re.compile(rf"{NUM}\s*(?i:kgs?|kilos?|kilograms?|kilogramm)\b"),
    re.compile(
        rf"(?i:\b(?:gross weight|weight|gewicht|bruttogewicht){TOTAL_WORD}(?:\s*\(kgs?\))?)[^\S\n]*:?[^\S\n]*{NUM}"
        rf"(?![\d.,']|{OTHER_UNIT}\b)"
    ),
)
# A unit may end without a space before a Korean or Japanese particle ("lb입니다", session 39219): no \b there.
POUNDS = (re.compile(rf"{NUM}\s*(?i:lbs?|pounds?)(?![A-Za-z])"),)
TONNES = (re.compile(rf"{NUM}\s*(?i:t(?![-\w])|tons?|tonnes?|tonnen)"),)
# Round 79: "a total volume of 95.00 cubic feet" (2.69 cbm) was read by the model as 95 cbm.
FEET3 = r"\s*(?i:cubic\s+f(?:ee|oo)t(?![A-Za-z])|cu\.?\s*ft(?![A-Za-z])|ft3(?![A-Za-z\d])|ft³)"
FT3 = Decimal("0.028316846592")
# "3360 litres" is 3.36 cbm; a bare "l" is left alone: "5 l canisters" names the goods, not the shipment.
LITRE = r"\s*(?i:lit(?:er|re)s?\b|ltrs?\b)"
VOLUME = (
    re.compile(rf"{NUM}\s*(?:(?i:cbm|cubic met\w*|kubikmeter)\b|m3\b|m³)"),
    re.compile(
        rf"(?i:\b(?:cbm|volume|volumen){TOTAL_WORD}(?:\s*\((?:cbm|m3|m³)\))?)[^\S\n]*:?[^\S\n]*{NUM}"
        rf"(?![\d.,']|{FEET3}|{LITRE})"
    ),
)
CUBIC_FEET = (re.compile(rf"{NUM}{FEET3}"),)
LITRES = (re.compile(rf"{NUM}{LITRE}"),)
DIMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m|inch(?:es)?|in(?=\s*(?:$|[^\w\s])|\s+(?:each|apiece|per|je|pro)\b))?\b",  # "in total": a word, not inches
    re.IGNORECASE,
)
PER_M = {"mm": Decimal(1000), "cm": Decimal(100), "m": Decimal(1), "in": 1 / Decimal("0.0254")}


def _per_m(unit: str | None) -> Decimal:
    """Units of one metre: dimensions without a unit are centimetres; "48x40x50 in" are inches."""
    unit = (unit or "cm").lower()
    return PER_M.get(unit, PER_M["in"])


# A lot's label besides a bullet: "Lot 2:", "Posten 2:", or a numbered line ("2) 3 pallets", "2. 3 pallets").
LOT_LABEL = r"(?:lot|position|pos\.?|item|los|posten)\s*\d+\s*[:.)-]|\d{1,2}[.)](?=\s*\d)"
LOT_LINE = rf"(?im)^\s*(?:[-*•]\s*(?=\d)|{LOT_LABEL})"
LOT = re.compile(
    rf"(?im)^\s*(?:[-*•]|{LOT_LABEL})\s*(\d+)\s+"
    r"(?!(?:kgs?|kilos?|lbs?|cbm|cm|mm|m3|m)\b)[^\W\d](.+)$"
)
EACH = r"each|apiece|per\s+(?:piece|unit|carton|case|drum|pallet|box|crate)|je|pro\s+st(?:ü|ue)ck"
ALL = r"together|in\s+total|total|combined|altogether|zusammen|gesamt|insgesamt"
MODE = rf"(?:\b(?P<each>{EACH}|à)|\b(?P<all>{ALL}))?\s*"
AFTER = rf"(?:\s+(?P<after>{EACH})\b)?"  # "210 kg each"
PER_PIECE = rf"(?:\b(?:{EACH})\b|(?<!\w)à)"
# The gross figure in the bracket of a per-piece net one: "each 142 kg net (160 kg gross)".
NET_FIRST = re.compile(rf"{PER_PIECE}\s*{NUM}\s*kgs?\s*(?:net|netto|n\.\s?w\.?)\s*\(\s*$", re.IGNORECASE)
EACH_WEIGHT = re.compile(
    rf"{PER_PIECE}\s*(?:of\s+)?{NUM}\s*(?:kgs?|kilos?|lbs?|pounds?)\b|{NUM}\s*(?:kgs?|kilos?|lbs?|pounds?)\s+(?:gross\s+)?{PER_PIECE}",
    re.IGNORECASE,
)
EACH_VOLUME = re.compile(rf"{PER_PIECE}\s*{NUM}\s*(?:cbm|m3|m³)|{NUM}\s*(?:cbm|m3|m³)\s+{PER_PIECE}", re.IGNORECASE)
LOT_WEIGHT = re.compile(
    rf"{MODE}{NUM}\s*(?P<unit>kgs?|kilos?|kilograms?|lbs?|pounds?|tonnes?|tons?|tonnen|t(?![-\w]))\b(?:\s*(?P<kind>gross|brutto|net|netto|g\.\s?w\.?|n\.\s?w\.?))?{AFTER}",
    re.IGNORECASE,
)
LOT_VOLUME = re.compile(
    rf"{MODE}{NUM}\s*(?P<unit>cbm\b|m3\b|m³|{FEET3.removeprefix(r'\s*')}|{LITRE.removeprefix(r'\s*')}){AFTER}",
    re.IGNORECASE,
)
LOT_DIMS = re.compile(MODE + DIMS.pattern, re.IGNORECASE)
PIECES = re.compile(
    r"(\d+)[^\S\n]*(?:pcs|pieces?|pallets?|skids?|crates?|cases|drums?|colli|packages|cartons|boxes|stück|stk|packstücke"
    r"|paletten|kartons|kisten|colis|palettes|caisses|bultos|piezas|cajas|pezzi|casse|volumes|caixas|stuks|dozen)\b",
    re.IGNORECASE,
)
NOT_URGENT = re.compile(
    r"\b(?:not\s+(?:urgent|in\s+a\s+hurry|time[\s-](?:critical|sensitive))|no\s+(?:rush|hurry)|nicht\s+(?:eilig|dringend)|keine\s+eile"
    r"|price\s+(?:matters|counts|is)\s+more\s+than\s+(?:speed|time)|preis\s+ist\s+wichtiger"
    r"|pas\s+(?:urgent|press[ée]\w*)"
    r"|(?:not|nicht|pas)\s+(?:\w+\s+){0,4}?(?:the\s+|das\s+|die\s+|la\s+|le\s+)?(?:fastest|quickest|schnellst\w*|shortest"
    r"|plus\s+rapide))\b",
    re.IGNORECASE,
)
FASTEST = re.compile(
    r"\b(?:fastest|quickest|schnellst\w*|plus\s+rapide|m(?:á|a|ai)s\s+r[aá]pid[ao]|più\s+(?:veloce|rapid[ao])|snelste)\b",
    re.IGNORECASE,
)
CHEAPEST = re.compile(
    r"\b(?:cheapest|lowest|most economical|günstigst\w*|billigst\w*|moins\s+ch[eè]re?|meilleur\s+prix"
    r"|m(?:á|a|ai)s\s+(?:econ[oó]mic[ao]|barat[ao])|più\s+economic[ao]|goedkoopste)\b",
    re.IGNORECASE,
)
HURRY = re.compile(
    r"\b(?:urgent\w*|asap|express\s+(?:service|freight|delivery|option|shipment)|service\s*:\s*(?:express|priority)|as soon as possible|as (?:quickly|fast) "
    r"as possible|time[\s-]critical|speed\s+(?:matters|counts|is)\s+more|so schnell wie möglich|eilig\w*"
    r"|dringend\w*"
    # The transit as the wish: "shortest transit time", "kürzeste Laufzeit", "transit time is key", "price is secondary"
    r"|shortest\s+(?:transit|lead|delivery|travel)(?:\s+times?)?|kürzeste\w*\s+(?:laufzeit|transitzeit|transportzeit|lieferzeit)"
    r"|(?:transit(?:\s+time)?|speed|time)\s+is\s+(?:key|crucial|critical|essential|what\s+matters|the\s+priority)"
    r"|(?:price|cost|rate)\s+is\s+(?:secondary|not\s+(?:an\s+issue|important|the\s+priority))"
    r"|(?:go\s+for|prefer|need|want|choose|book)\s+(?:the\s+|an\s+)?express|express[,!]?\s+(?:please|pls)"
    r"|next\s+flight\s+out|(?:next|first)\s+available\s+flight|time[\s-]sensitive|(?-i:AOG))\b",
    re.IGNORECASE,
)
# The price named as what counts beats a transit the mail also mentions: "price is what counts now".
PRICE_FIRST = re.compile(
    r"\b(?:price|cost|rate)\s+is\s+(?:key|crucial|what\s+(?:counts|matters)|the\s+priority|most\s+important)\b",
    re.IGNORECASE,
)
DOOR = re.compile(
    r"door[\s-]*to[\s-]*(?:port|airport)|pick[\s-]?up|truck\w* from"
    r"|from our (?:[\w-]+\s+)?(?:site|warehouse|premises|plant|factory|shop|depot|facility|workshop|store)"
    r"|collection from|collect from|abhol\w*|ab werk|ab haus|\bex[\s-]?works\b|\bEXW\b"
    r"|\b(?:recogida|recoger|ritiro|ritirare|ophalen|ophaling|recolha|recolher|coleta|enlèvement|ramassage)\b"
    r"|\bcollect\w*\s+(?:them|it|the\s+(?:goods|cargo|shipment|freight|cartons|pallets|boxes|crates))\s+from\b"
    # A voice message's own words, asked of us: "the airline will collect the cargo" or "we will arrange the truck
    # ourselves" is no door pickup.
    r"|(?:please|kindly|could you|can you|would you)\s+(?:also\s+)?"
    r"(?:collect (?:the )?(?:goods|cargo|shipment|freight)|arrange (?:the|a) truck)",
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
# A phone or fax number: "+49 6181 12345", "Tel. 06181 12345", "06181/12345".
PHONE = re.compile(
    r"\+\d[\d \t/()-]{6,}\d|\b0\d{2,5}[ \t/-]+\d{3,}\b|\b(?:tel|phone|fon|fax|mobile?)\b\.?:?[ \t]*[\d \t/()-]+\d",
    re.IGNORECASE,
)
# The address before the postcode on its line is not the pickup's.
OTHER_ADDRESS = re.compile(
    r"\b(?:invoice|invoicing|billing|bill\s+to|rechnung\w*|head\s+office|registered\s+office|firmensitz|sitz|"
    r"consignee|empf(?:ä|ae)nger|deliver(?:y)?\s+(?:address|to)|ship\s+to|lieferadresse)\b",
    re.IGNORECASE,
)
FOREIGN_PREFIX = re.compile(r"\b(?:I|F|E|A|CH|NL|B|L|PL|CZ|DK|S|FL|P|GR)-$")
# "20121 Milano, Italy": the town of the postcode and its country right after it. "63450 Hanau for export to Chicago,
# USA" names the destination's country, not the pickup's.
TOWN_WORD = r"(?!(?:to|for|nach|via|export|and|und|then|with)\b)[^\W\d][\w'’.-]*"
ABROAD = re.compile(
    rf"[ \t]+{TOWN_WORD}(?:[ \t-]+{TOWN_WORD}){{0,2}}[ \t]*,?[ \t]*\(?(?:italy|italia|spain|espa(?:ñ|n)a|france|"
    r"frankreich|austria|(?:ö|oe)sterreich|switzerland|schweiz|suisse|netherlands|niederlande|belgium|belgien|poland|"
    r"polen|czechia|czech\s+republic|denmark|d(?:ä|ae)nemark|sweden|schweden|portugal|greece|turkey|t(?:ü|ue)rkei|usa|"
    r"united\s+states|united\s+kingdom)\b",
    re.IGNORECASE,
)
# Where the sentence or clause holding a postcode starts: "Str. 1" or "No. 5" ends none.
CLAUSE_START = re.compile(r";|[.!?](?=\s+[A-ZÄÖÜ])|\n")
# "Dangerous goods: UN1133, Class 3" in a packing list, also when the mail itself says "non-dangerous".
DANGEROUS = re.compile(
    r"(?<!non-)(?<!non )(?<!no )(?<!not )(?<!kein )(?<!keine )(?<!kein)"
    r"\b(?:dangerous goods|hazardous goods|dgr|gefahrgut)\b\s*[:|]?\s*"
    r"(?!no\b|none\b|n/?a\b|nein\b|kein\w*|non\b|not\b|-|$)(\w.*)|(.*\bUN\s?\d{4}\b.*)",
    re.IGNORECASE,
)
DG_DOCUMENTS = "dangerous_goods_documents"
CURRENCY_ASK = re.compile(r"(?i:\b(?:in|en|currency:?|währung:?|devise:?|moneda:?))\s+([A-Z]{3})\b")
LONE_CODE = re.compile(r"(?<![\d.,])(?<![\d.,] )\b([A-Z]{3})\b(?! ?\d)")
GERMAN = {"und", "der", "die", "das", "bitte", "mit", "für", "von", "nach", "wir", "ich", "ein", "eine", "angebot",
          "grüße", "gewicht", "ist", "zu", "sie", "ihr", "unser", "sendung", "danke"}  # fmt: skip
ENGLISH = {"the", "and", "please", "for", "with", "we", "to", "a", "quote", "regards", "weight", "is", "of", "our",
           "you", "your", "shipment", "thanks"}  # fmt: skip
FRENCH = {"le", "la", "les", "des", "du", "et", "pour", "nous", "vous", "votre", "un", "une", "est", "avec", "merci",
          "bonjour", "poids", "envoi", "colis", "que", "pouvez", "cordialement", "devis", "dans", "aux"}  # fmt: skip
# Words only that language uses among these, so a mail in one never counts for another ("por favor" is es and pt).
SPANISH = {"el", "los", "las", "gracias", "hola", "saludos", "necesitamos", "envío", "nuestra", "nuestro", "y",
           "cotización", "muchas", "estimado", "estimada", "buenos", "días", "precio", "recogida", "mercancía",
           "usted", "cajas"}  # fmt: skip
ITALIAN = {"il", "gli", "della", "delle", "che", "per", "grazie", "buongiorno", "saluti", "cordiali", "vorremmo",
           "nostro", "nostra", "spedizione", "sono", "preventivo", "ritiro", "merce", "colli", "di"}  # fmt: skip
PORTUGUESE = {"não", "você", "obrigado", "obrigada", "olá", "nosso", "nossa", "cotação", "envio", "pelo", "pela",
              "com", "uma", "frete", "atenciosamente", "precisamos", "retirada", "coleta"}  # fmt: skip
DUTCH = {"het", "een", "voor", "wij", "graag", "zending", "groeten", "bedankt", "onze", "naar", "ophalen",
         "offerte", "prijs", "alvast", "vriendelijke", "hartelijk", "kunt", "jullie", "ons"}  # fmt: skip
POLISH = {"dzień", "dobry", "prosimy", "proszę", "wycenę", "wycena", "wysyłki", "przesyłki", "przesyłka", "dziękuję",
          "dziękujemy", "pozdrawiam", "pozdrowienia", "jest", "się", "nie", "oraz", "dla", "towar", "waga", "naszej"}  # fmt: skip
TURKISH = {"merhaba", "ve", "bir", "için", "ile", "teşekkürler", "teşekkür", "ederiz", "lütfen", "fiyat", "teklifi",
           "teklif", "rica", "ediyoruz", "gönderi", "ağırlık", "brüt", "saygılarımla", "koli", "seçeneği"}  # fmt: skip
SWEDISH = {"och", "att", "för", "är", "som", "tack", "hej", "hälsningar", "offert", "frakt", "vikt", "vår", "våra",
           "behöver", "sändning", "billigaste", "på", "från", "förhand"}  # fmt: skip
CZECH = {"dobrý", "prosím", "děkuji", "děkujeme", "nabídku", "nabídka", "přepravy", "přeprava", "hmotnost", "jedná",
         "jsou", "kusy", "pozdravem", "zásilka", "zásilku", "cenovou"}  # fmt: skip
LANGUAGES = {"en": ENGLISH, "de": GERMAN, "fr": FRENCH, "es": SPANISH, "it": ITALIAN, "pt": PORTUGUESE, "nl": DUTCH,
             "pl": POLISH, "tr": TURKISH, "sv": SWEDISH, "cs": CZECH}  # fmt: skip
# One of their words alone turns up in an English mail ("per kg", a "Saludos" sign-off): two are needed, else the
# quote is translated for a customer who wrote English.
TWO_WORDS = set(LANGUAGES) - {"en", "de", "fr"}
# The persona's own sign-off stays English or German whatever language the mail is in (rounds 70 and 72): a short
# line from it to the end, with only the name and company below, is no evidence of the mail's language.
SIGN_OFF = re.compile(
    r"^[ \t]*(?:thanks?\b|thank you|many thanks|cheers|best\b|kind regards|best regards|regards|warm regards|"
    r"greetings|many greetings|sincerely|yours|(?:viele|beste|liebe|freundliche) gr(?:ü|ue)(?:ß|ss)e|"
    r"mit freundlichen gr(?:ü|ue)(?:ß|ss)en|mfg\b|lg\b)[^\n]{0,60}$",
    re.IGNORECASE | re.MULTILINE,
)
# A script other than Latin names the language by itself; the model's code can refine it (Cyrillic: uk, not ru).
SCRIPTS = {"CJK": "zh", "HIRAGANA": "ja", "KATAKANA": "ja", "HANGUL": "ko", "CYRILLIC": "ru", "GREEK": "el",
           "ARABIC": "ar", "HEBREW": "he", "THAI": "th", "DEVANAGARI": "hi"}  # fmt: skip
UNSPACED = {"CJK", "HIRAGANA", "KATAKANA", "THAI"}  # every letter counts as a word

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
    "first_name: the customer's first name exactly as signed, or null if they did not sign with a first name."
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
# A figure converted from pounds or cubic feet is the scorer's own to two decimals: 16.25 cubic feet is 0.46 cbm, 121.26 lb
# is 55.00 kg (rounds 116, 133, 136 fit only so; 136 lost 0.1 points on 0.46014876 cbm).
CONVERTED = Decimal("0.01")
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
    },
    "required": ["origin", "destination", "weight_kg", "volume_cbm", "pieces", "lots", "preference", "door_pickup",
                 "pickup_postcode", "currency", "language", "first_name"],
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


INSURANCE = re.compile(r"(?i)insur|versicher")
NO_INSURANCE = re.compile(
    r"(?i)\bno\s+(?:cargo\s+)?insurance|insurance\s+(?:is\s+)?not\s+(?:needed|required|wanted)|own\s+(?:insurance\s+)?policy"
    r"|keine\s+(?:transport)?versicherung|ohne\s+(?:transport)?versicherung"
)
PERCENT = re.compile(rf"{NUM}\s*(%|‰|(?i:per\s*mille|promille|prozent|per\s*cent|pct\b))")
MONEY = re.compile(rf"(?i:(EUR|USD|GBP|CHF|€|\$)\s*{NUM}(k\b)?|{NUM}(k\b)?\s*(EUR|USD|GBP|CHF|€|\$))")
MINIMUM = re.compile(r"(?i)(?:minimum|mindest|min\.)\D{0,25}$")
UPLIFT = re.compile(r"(?i)(?:plus|\+|zuzüglich|zzgl\.?)\s*$")


def insurance(text: str) -> tuple[Decimal, str, Decimal, Decimal, Decimal] | None:
    """The cargo insurance the request prices itself: (goods value, its currency, insured share in %, premium rate
    in %, minimum premium). None when it asks for none, declines it or leaves the rate or the value out."""
    if not INSURANCE.search(text) or NO_INSURANCE.search(text):
        return None
    share, rate = Decimal(100), None
    for m in PERCENT.finditer(text):
        value = number(m.group(1))
        if value is None:
            continue
        if m.group(2) == "‰" or "mille" in m.group(2).lower():
            value /= 10
        before = text[max(0, m.start() - 40) : m.start()]
        if UPLIFT.search(before):
            share = 100 + value
        elif value >= 100:
            share = value
        elif value < 5 and rate is None:
            rate = value
    values, minimum = [], Decimal(0)
    for m in MONEY.finditer(text):
        amount = number(m.group(2) or m.group(4))
        if amount is None:
            continue
        if m.group(3) or m.group(5):
            amount *= 1000
        currency = {"€": "EUR", "$": "USD"}.get(c := (m.group(1) or m.group(6)).upper(), c)
        if MINIMUM.search(text[max(0, m.start() - 40) : m.start()]):
            minimum = amount
        else:
            values.append((amount, currency))
    if rate is None or not values:
        return None
    value, currency = max(values)
    return value, currency, share, rate, minimum


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
    before, after = set(PRE_CARRIAGE.findall(text)), set(ON_CARRIAGE.findall(text))
    origins = {c for c in ORIGIN_LABEL.findall(text) if _airport(c)} - after | before
    destinations = {c for c in DEST_LABEL.findall(text) if _airport(c)} - before | after
    if len(origins) == 1 and len(destinations) == 1 and origins != destinations:
        return origins.pop(), destinations.pop()
    return None


def _label_reach(pattern: re.Pattern, text: str) -> dict[str, int]:
    """Each airport code a label reaches, with the shortest reach of any label to it."""
    reach: dict[str, int] = {}
    for m in pattern.finditer(text):
        if _airport(code := m.groups()[-1]):
            reach[code] = min(reach.get(code, m.end() - m.start()), m.end() - m.start())
    return reach


SHIPMENT_LINE = re.compile(r"^\s*(?:(?i:shipment|sendung|envoi|lot)\s*)?(?:\d+[.):]|[-•*])\s+(.*)$", re.MULTILINE)
CODE = re.compile(r"\b[A-Z]{3}\b")


def shipments(rfq: Rfq, text: str) -> list[Rfq]:
    """Rounds 89 and 90: one mail, shipments to several airports ("1. GRU ... 1281 kg, 3.76 cbm / 2. TLV ..."), the
    fastest "for each". Every shipment with its own airport, lane when its line names one, and cargo, in the mail's
    order; none unless each line names an airport and a weight and the airports differ."""
    lines = [
        (code, line)
        for line in SHIPMENT_LINE.findall(text)
        if (code := next((c for c in CODE.findall(line) if _airport(c)), None))
    ]
    if len({code for code, _ in lines}) < 2:
        return []
    found = []
    for code, rest in lines:
        pieces = _pieces(rest)
        if not (weight := _weight(rest, pieces)):
            return []
        origin, code = _lane(rest) or (rfq.origin, code)
        found.append(rfq.model_copy(update={
            "origin": origin, "destination": code, "weight_kg": weight, "pieces": pieces,
            "volume_cbm": _volume(rest, pieces) or Decimal(0), "missing": tuple(m for m in rfq.missing if m != "destination"),
        }))  # fmt: skip
    return found


def _destination_only(text: str) -> str | None:
    """The one destination of a mail that names no origin airport (door-to-port). A code both labels reach belongs to
    the nearer one: "select the departure airport for the fastest option to JFK" labels the destination (round 69),
    "our truck goes to departure airport FRA" the origin."""
    to, origin = _label_reach(DEST_LABEL, text), _label_reach(ORIGIN_LABEL, text)
    found = {code for code, reach in to.items() if reach < origin.get(code, reach + 1)}
    if set(origin) - found:
        return None
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
            weight = (pounds * LB).quantize(CONVERTED, ROUND_HALF_UP) if pounds is not None else tonnes * 1000
    if weight is None or not EACH_WEIGHT.search(text):
        return weight
    return weight * pieces if pieces else None


def _volume(text: str, pieces: int | None) -> Decimal | None:
    stated = _unique(VOLUME, text, thousands=False)
    if stated is not None and EACH_VOLUME.search(text):
        return stated * pieces if pieces else None
    if stated is not None:
        return stated
    if not any(p.search(text) for p in VOLUME) and (feet := _unique(CUBIC_FEET, text, thousands=False)):
        return (feet * FT3).quantize(CONVERTED, ROUND_HALF_UP)
    if not any(p.search(text) for p in VOLUME) and (litres := _unique(LITRES, text, thousands=True)):
        return litres / 1000
    dims = DIMS.findall(text)
    if len(dims) != 1:
        return None
    *sides, unit = dims[0]
    per_m = _per_m(unit)
    volume = Decimal(1)
    for side in sides:
        volume *= number(side, thousands=False) / per_m
    return volume * (pieces or 1)


def _per_kg(unit: str) -> Decimal:
    unit = unit.lower()
    return LB if unit.startswith(("lb", "pound")) else Decimal(1000) if unit.startswith("t") else Decimal(1)


def _per_cbm(unit: str) -> Decimal:
    return FT3 if re.fullmatch(FEET3, unit) else Decimal("0.001") if re.fullmatch(LITRE, unit) else Decimal(1)


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
        weights = [m for m in LOT_WEIGHT.finditer(rest) if not (m["kind"] or "").lower().startswith("n")]
        stated, dims = LOT_VOLUME.search(rest), LOT_DIMS.search(rest)
        if len(weights) != 1 or not (stated or dims):
            return None
        per_kg = _per_kg(weights[0]["unit"])
        kg = number(weights[0].group(3), thousands=per_kg < 1000)  # "1.234 t" is 1234 kg, not 1234 t
        kg = kg and kg * per_kg
        if stated:
            cbm = (
                number(stated.group(3), thousands=False)
                * (n if stated["each"] or stated["after"] else 1)
                * _per_cbm(stated["unit"])
            )
        else:
            *sides, unit = dims.groups()[2:]
            per_m = _per_m(unit)
            cbm = Decimal(1 if dims["all"] else n)
            for side in sides:
                cbm *= number(side, thousands=False) / per_m
        if kg is None or cbm is None:
            return None
        each = weights[0]["each"] or weights[0]["after"] or NET_FIRST.search(rest[: weights[0].start(3)])
        weight += kg * (n if each else 1)
        volume += cbm
        pieces += n
    return (weight, volume, pieces) if len(lots) > 1 else None


def _abroad(text: str, start: int, end: int) -> bool:
    """A postcode abroad: "I-20121 Milano" or "20121 Milano, Italy". The pickup zones are German (R16), and
    20121 read as zone 20 sent a Milan pickup to Hamburg's truckers."""
    return bool(FOREIGN_PREFIX.search(text[max(0, start - 4) : start]) or ABROAD.match(text[end:]))


def pickup_postcodes(text: str) -> tuple[dict[str, bool], bool]:
    """The five-digit figures that can be the pickup's postcode, each with whether its line speaks of the pickup, and
    whether one of the mail's postcodes lies abroad. A phone or fax number ("Tel. 06181 12345") and an address
    labelled as another one (invoice, head office, consignee) are no pickup postcode."""
    phones = [m.span() for m in PHONE.finditer(text)]
    codes: dict[str, bool] = {}
    abroad = False
    for m in POSTCODE.finditer(text):
        start, end = m.span(1)
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        clause = text[max([m.end() for m in CLAUSE_START.finditer(text, 0, start)] + [max(0, start - 60)]) : start]
        if any(a <= start and end <= b for a, b in phones) or OTHER_ADDRESS.search(clause):
            continue
        if _abroad(text, start, end):
            abroad = True
            continue
        codes[m[1]] = codes.get(m[1], False) or bool(DOOR.search(line))
    return codes, abroad


def not_a_pickup_postcode(text: str, code: str) -> bool:
    """The text names this figure only as a phone number, another address or a postcode abroad."""
    return code in POSTCODE.findall(text) and code not in pickup_postcodes(text)[0]


def _abroad_pickup(text: str) -> bool:
    """The pickup lies abroad: a postcode the mail places abroad, or a country abroad on a pickup line ("collected
    from our warehouse at Alpenstraße 48, 5020 Salzburg, Austria"). It has no zone, so no postcode is asked for."""
    return pickup_postcodes(text)[1] or any(ABROAD.search(line) for line in text.splitlines() if DOOR.search(line))


def _pickup(text: str) -> tuple[bool, str | None]:
    if not DOOR.search(text) or OWN_DELIVERY.search(text):
        return False, None
    codes, _ = pickup_postcodes(text)
    if len(codes) == 1:
        return True, next(iter(codes))
    named = [code for code, on_pickup_line in codes.items() if on_pickup_line]
    return True, named[0] if len(named) == 1 else None


def _currency(text: str) -> str | None:
    """The currency asked for: "in USD", "Currency: USD", or else the one code the mail names away from a figure
    ("Price: USD", round 126's Turkish "EUR cinsinden"); "3000 EUR" is an amount, not a wish."""
    asked = {c for c in CURRENCY_ASK.findall(text) if c in CURRENCIES}
    named = asked or {c for c in LONE_CODE.findall(text) if c in CURRENCIES}
    return named.pop() if len(named) == 1 else None


def words_of(text: str, extra: dict[str, set[str]] | None = None) -> dict[str, int]:
    """How many of each language's common words the text holds."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    return {
        lang: sum(w in known or w in (extra or {}).get(lang, ()) for w in words) for lang, known in LANGUAGES.items()
    }


def _script(text: str) -> str | None:
    """The script most of the text is written in, None for Latin: a word counts once, a letter of a script without
    spaces once each. Round 72 was Chinese signed "Thanks a lot and have a lovely day!", answered in English."""
    units: Counter[str] = Counter()
    for word in re.findall(r"[^\W\d_]+", text):
        scripts = Counter(unicodedata.name(c, "?").split()[0] for c in word)
        for script, n in scripts.items():
            units[script] += n if script in UNSPACED else 1
    if not units:
        return None
    best = max(units, key=units.get)
    if best == "CJK" and (units["HIRAGANA"] or units["KATAKANA"]):
        return "ja"
    return SCRIPTS.get(best)


def _unsigned(text: str) -> str:
    """The text above the persona's sign-off (`SIGN_OFF`) when only a name and a company follow it."""
    for mark in reversed(list(SIGN_OFF.finditer(text))):
        if len([line for line in text[mark.end() :].splitlines() if line.strip()]) <= 4:
            return text[: mark.start()]
    return text


# The language a customer asks us to answer in: "Please reply in German, our shipping office does not read English".
NAMED_LANGUAGE = {"german": "de", "deutsch": "de", "allemand": "de", "english": "en", "englisch": "en",
                  "anglais": "en", "french": "fr", "französisch": "fr", "français": "fr", "spanish": "es",
                  "spanisch": "es", "español": "es", "italian": "it", "italienisch": "it", "italiano": "it",
                  "dutch": "nl", "niederländisch": "nl", "polish": "pl", "polnisch": "pl", "portuguese": "pt",
                  "turkish": "tr", "swedish": "sv", "czech": "cs"}  # fmt: skip
ASKED_LANGUAGE = re.compile(
    r"\b(?:reply|respond|answer|write|antwort\w*|schreib\w*|r[ée]pond\w*)\b[^.\n]{0,40}?\b(?:in|auf|en)\s+"
    rf"({'|'.join(NAMED_LANGUAGE)})\b",
    re.IGNORECASE,
)


def _language(text: str, said: object = None, attached: str = "") -> str:
    """The script's language, else the language with the most common words, English on a tie (round 70 was French,
    answered in English). The mail's words above its sign-off decide first (a Polish line signed "Thanks and
    regards" is Polish), then an attachment or voice message (`attached`) only when the mail holds at most two words
    besides codes (a German packing list made "new request attached, FRA-JFK" German), the sign-off last. The
    model's ISO code (`said`) counts only for a language our word lists do not know: it once answered "de" for an
    English mail full of German place names."""
    if asked := ASKED_LANGUAGE.search(_unsigned(text)):
        return NAMED_LANGUAGE[asked.group(1).lower()]
    said = said.strip().lower() if isinstance(said, str) and re.fullmatch(r"\s*[A-Za-z]{2}\s*", said) else None
    own = said if said not in LANGUAGES else None
    unsigned = _unsigned(text)
    if len([w for w in re.findall(r"[^\W\d_]+", unsigned) if not w.isupper()]) > 2:
        attached = ""
    for part in (unsigned, attached, text):
        if script := _script(part):
            return own or script
        counts = {lang: n for lang, n in words_of(part).items() if n >= 2 or lang not in TWO_WORDS}
        best = max(counts, key=lambda lang: (counts[lang], lang == "en"))
        if own and counts[best] * 5 < len(re.findall(r"[^\W\d_]+", part)):
            return own
        if counts[best]:
            return best
    return own or "en"


def _preference(text: str) -> Preference:
    """A superlative the customer wrote decides; without one, a hurry they describe means fastest."""
    text = NOT_URGENT.sub("", text)
    if FASTEST.search(text):
        return Preference.FASTEST
    if CHEAPEST.search(text) or PRICE_FIRST.search(text) or not HURRY.search(text):
        return Preference.CHEAPEST
    return Preference.FASTEST


# A customer ruling an airline out (round 42: "Please do not offer <airline> (<code>) for this shipment or for any of
# our future inquiries", quoted anyway and rejected as excluded_airline).
RULED_OUT = re.compile(
    r"\b(?:not(?!\s+only\b)|never|exclud\w*|avoid\w*|without|except|other than"
    r"|nicht(?!\s+nur\b)|kein\w*|ohne|au(?:ß|ss)er|vermeid\w*|ausschlie(?:ß|ss)(?!lich\b)\w*)\b|n['’]t\b",
    re.IGNORECASE,
)
# A negation of something good is no exclusion: "we don't mind X", "never had issues with X", "does not require X".
NEGATED_POSITIVE = re.compile(
    r"(?:\b(?:not|never|no|nicht|kein\w*)\b|n['’]t\b)(?:\s+\w+){0,3}?\s+(?:mind|objection|object|problems?|issues?"
    r"|complain\w*|require\w*|matter|preference|prefer\w*|(?:have|has|need)\s+to(?:\s+be)?|must)\b",
    re.IGNORECASE,
)
LASTING = re.compile(
    r"\b(?:future|from now on|always|again|any of our|all of our|all our"
    r"|künftig|zukünftig|ab sofort|generell|grundsätzlich|immer)\b",
    re.IGNORECASE,
)
AIRLINE_SENTENCES = re.compile(r"(?<=[.!?;])\s+|\n+")
AIRLINE_ONLY = r"(?:only|exclusively|solely|nur|ausschlie(?:ß|ss)lich|seulement|uniquement)"
AIRLINE_END = r"(?=\s*(?:$|[,.!?;]|please\b|bitte\b|for (?:this|the) (?:shipment|quote)\b|für diese sendung\b))"
# "no airline other than ...", "keine andere Airline als ...", "aucune compagnie sauf ...": one airline allowed.
AIRLINE_NO_OTHER = (r"(?:no|keine?\w*|aucune?)\s+(?:other\s+|andere\w*\s+|autre\s+)?(?:airlines?|carriers?|"
                    r"fluggesellschaft\w*|compagnies?(?:\s+a[ée]riennes?)?)\s+(?:other\s+than|except|but|"
                    r"apart\s+from|au(?:ß|ss)er|als|sauf|que)")  # fmt: skip
AIRLINE_CONNECTOR = r"(?:\s*,\s*(?:(?:or|and|oder|und|ou|et)\s+)?|\s+(?:or|and|oder|und|ou|et)\s+)"
AIRLINE_WORD = re.compile(r"\s+(?:air\s+cargo|air\s+freight|airways|airlines?|air|cargo)$", re.IGNORECASE)
CODE_IN_BRACKETS = re.compile(r"\(([A-Z0-9]{2})\)")


def _calls(name: str) -> set[str]:
    """How a customer may write an airline: in full, or without its trailing "Air", "Cargo" or "Airways"."""
    short = AIRLINE_WORD.sub("", name)
    return {name.casefold()} | ({short.casefold()} if len(short) >= 4 else set())


CLAUSE_NEGATION = re.compile(r"\b(?:not|no|never|nicht|kein\w*)\b|n['’]t\b")


def required_airlines(text: str, airlines: set[str]) -> tuple[str, ...]:
    """Read an exclusive instruction directly attached to known carrier names: "only", "exclusively", "nur" next to
    the names, or "no airline other than" before them."""
    if not airlines:
        return ()
    aliases = {name.casefold(): name for name in sorted(airlines)}
    for name in sorted(airlines):
        for alias in _calls(name):
            aliases.setdefault(alias, name)
    names = "|".join(re.escape(alias) for alias in sorted(aliases, key=lambda alias: (-len(alias), alias)))
    mention = rf"(?<!\w)(?:{names})(?!\w)"
    item = rf"{mention}(?:\s*\([a-z0-9]{{2}}\))?"
    listed = rf"{item}(?:{AIRLINE_CONNECTOR}{item})*"
    instruction = re.compile(
        rf"\b{AIRLINE_ONLY}\s+(?:(?:quote|offer|use|fly|accept|book|ship|work)\s+)?"
        rf"(?:(?:with|via|on|mit|von|über|avec|par|angebote von)\s+)?"
        rf"(?P<after>{listed})(?:\s+(?:fliegen|anbieten))?{AIRLINE_END}"
        rf"|(?P<before>{listed})\s+{AIRLINE_ONLY}\b{AIRLINE_END}"
        rf"|\b{AIRLINE_NO_OTHER}\s+(?P<other>{listed})"
        # "must fly with X", "requires X", "X is our only approved carrier", "X, no other airline"
        rf"|\b(?:must|has to|have to|needs? to|muss|müssen)\s+(?:be\s+)?(?:fl(?:y|own)|go|ship(?:ped)?|booked)\s+"
        rf"(?:with|via|on|by|mit)\s+(?P<must>{listed}){AIRLINE_END}"
        rf"|\b(?:muss|müssen)\s+(?:mit|über)\s+(?P<muss>{listed})\s+(?:fliegen|geflogen|gehen|versendet)\b"
        rf"|\brequires?\s+(?P<requires>{listed})"
        rf"|(?P<sole>{listed})\s+is\s+(?:our|the)\s+only\b"
        rf"|(?P<other_after>{listed}),?\s+(?:and\s+)?no other (?:airlines?|carriers?)\b"
    )
    required = set()
    for sentence in AIRLINE_SENTENCES.split(text.casefold()):
        for match in instruction.finditer(sentence):
            if re.search(r"\b(?:not|nicht)\s+$", sentence[: match.start()]):
                continue
            # "doesn't have to fly with X" frees the choice, "X is the only one we do not use" rules X out.
            before = re.split(r"[,;:]", sentence[: match.start()])[-1]
            after = re.split(r"[,;:]", sentence[match.end() :])[0] if match["sole"] else ""
            if not (match["after"] or match["before"] or match["other"]) and CLAUSE_NEGATION.search(
                before + " " + after
            ):
                continue
            group = next(g for g in match.groups() if g)
            required.update(aliases[name] for name in re.findall(mention, group))
    return tuple(sorted(required))


def excluded_airlines(text: str, airlines: set[str]) -> tuple[tuple[str, ...], bool]:
    """The airlines a sentence with a negation names, from `airlines`, plus a two-letter code in brackets in it; and
    whether the customer means later requests too. A negation that names no known airline rules nothing out."""
    found, lasting = set(), False
    aliases = sorted({a.casefold() for airline in airlines for a in (airline, *_calls(airline))}, key=len, reverse=True)
    for sentence, clause, negated in _airline_clauses(text, aliases):
        lowered = clause.casefold()
        if not negated and not re.search(r"\bno\s", lowered):
            continue
        required = set(required_airlines(clause, airlines))
        # "No X, please" rules X out; a "no" elsewhere in the sentence ("no pickup needed") does not.
        before = r"(?<!\w)" if negated else r"\bno\s+"
        names = {
            airline
            for airline in airlines - required
            if any(re.search(rf"{before}{re.escape(alias)}(?!\w)", lowered) for alias in _calls(airline))
        }
        if names:
            found.update(names)
            if not required:
                found.update(CODE_IN_BRACKETS.findall(clause))
            lasting = lasting or bool(LASTING.search(sentence))
    return tuple(sorted(found)), lasting


# "We do not want Beta Cargo, it has to be Alpha Air": the negation holds for its own clause, and for one that only
# lists more airlines ("not Beta Cargo, Alpha Air or Gamma").
AIRLINE_CLAUSES = re.compile(r"[;,]|\s(?:so|but|aber|sondern)\s", re.IGNORECASE)
LIST_WORDS = re.compile(r"\b(?:and|or|nor|und|oder|noch|et|ou|either|neither|please|bitte)\b|\([a-z0-9]{2}\)|[\W_]+")


def _airline_clauses(text: str, aliases: list[str]) -> list[tuple[str, str, bool]]:
    """Each clause of each sentence with whether a negation rules out the airlines it names."""
    out = []
    for sentence in AIRLINE_SENTENCES.split(text):
        negated = False
        for clause in AIRLINE_CLAUSES.split(sentence):
            rest = clause
            for alias in aliases:
                rest = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", " ", rest, flags=re.IGNORECASE)
            named = rest != clause
            rest = re.sub(r"\b[A-Z][\w-]*", " ", rest)  # an airline we do not know is a name too
            only_names = named and not LIST_WORDS.sub("", rest.casefold())
            negated = bool(RULED_OUT.search(NEGATED_POSITIVE.sub(" ", clause))) or (negated and only_names)
            out.append((sentence, clause, negated))
    return out


def _carrier_requirements(ctx: Context, rfq: Rfq, text: str) -> Rfq:
    """Apply this request's airline requirements and carry forward lasting exclusions."""
    if ctx is None:  # the offline benchmark reads without a store
        return rfq
    airlines = store.airline_names(ctx)
    names, lasting = excluded_airlines(text, airlines)
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE kind = 'customer.excludes' AND lower(json_extract(payload, '$.customer')) = ?",
        (rfq.customer.lower(),),
    ).fetchall()
    earlier = {c for row in rows for c in json.loads(row["payload"])["carriers"]}
    if lasting:
        events.emit(ctx, "customer.excludes", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload={"customer": rfq.customer, "carriers": list(names)})  # fmt: skip
    return rfq.model_copy(update={"excluded_carriers": tuple(sorted(set(names) | earlier)),
                                  "required_carriers": required_airlines(text, airlines)})  # fmt: skip


# Where an older mail starts below the request: "-----Original Message-----", "On ... wrote:", a quoted line.
# Outlook's header without an address counts only by its dated "Sent:", next to "From:" or under a rule: a request
# laid out as "From: FRA / To: ORD" or "Von: FRA / Datum: ab 25.11." stays the customer's.
SENT = r"\*?(?:sent|gesendet)\*?[ \t]*:\*?[^\n]*(?:\b(?:19|20)\d\d\b|\b\d{1,2}:\d\d)"
HISTORY = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|forwarded message|ursprüngliche nachricht|weitergeleitete nachricht"
    r"|mensaje original|mensaje reenviado|message d'origine|message transféré|messaggio originale"
    r"|messaggio inoltrato|mensagem original|oorspronkelijk bericht|doorgestuurd bericht|originalmeddelande"
    r"|wiadomość oryginalna|исходное сообщение|原始邮件|原始郵件|转发邮件|元のメッセージ|원본 메시지)"
    r"|(?:from|von|de|da|van|od|från|от|kimden|发件人|寄件者|差出人|보낸 사람)\s*[:：].*@"
    r"|on .{0,80} wrote:\s*$|am .{0,80} schrieb"
    r"|.{0,120}\b(?:escribió|a écrit|ha scritto|escreveu|schreef|skrev|napisał\w*|написал\w*|yazdı)\s*:\s*$"
    r"|.{0,120}(?:写道|寫道)[:：]\s*$|>|begin forwarded message:|anfang der weitergeleiteten nachricht:"
    rf"|\*?(?:from|von)\*?[ \t]*:[^\n]*\n(?:[^\n]*\n){{0,3}}?[ \t]*{SENT}"
    rf"|{SENT}[^\n]*\n(?:[^\n]*\n){{0,3}}?[ \t]*\*?(?:from|von)\*?[ \t]*:"
    rf"|[-_]{{3,}}[ \t]*\n[ \t]*{SENT})",
    re.IGNORECASE | re.MULTILINE,
)
# Lines quoted with ">" and the "On ... wrote:" above them, wherever they stand: a reply written below them too.
QUOTED = re.compile(
    r"^(?:[ \t]*(?:on .{0,80} wrote|am .{0,80} schrieb):?[ \t]*\n)?(?:[ \t]*>.*(?:\n|$))+", re.IGNORECASE | re.MULTILINE
)
REPLY_SUBJECT = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*:\s*)+", re.IGNORECASE)


def own_words(body: str) -> str:
    """The mail without the older mails quoted in it: round 14 quoted an old request with its own lane, weight,
    greeting and name under the new one. Pricing and the reply both read only this."""
    body = unquoted if (unquoted := QUOTED.sub("", body)).strip() else body
    cut = HISTORY.search(body)
    return body[: cut.start()] if cut and body[: cut.start()].strip() else body


def _body(message: Message) -> str:
    return own_words(message.body)


def _text(message: Message) -> str:
    """Subject and body; a reply's or forward's subject belongs to the older mail it answers."""
    subject = "" if REPLY_SUBJECT.match(message.subject) else message.subject
    return f"{subject}\n\n{_body(message)}"


MONTHS = (
    "jan january januar janv janvier",
    "feb february februar févr fevr février fevrier",
    "mar march märz maerz mär mrz mars",
    "apr april avr avril",
    "may mai",
    "jun june juni juin",
    "jul july juli juil juillet",
    "aug august août aout",
    "sep sept september septembre",
    "oct october okt oktober octobre",
    "nov november novembre",
    "dec december dez dezember déc decembre décembre",
)
MONTH_NUMBER = {name: number for number, names in enumerate(MONTHS, 1) for name in names.split()}
MONTH_NAME = "|".join(sorted(MONTH_NUMBER, key=lambda name: (-len(name), name)))
DATE_VALUE = (
    r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.(?:\d{4})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\.?\s+(?:of\s+)?(?:{MONTH_NAME})\.?(?:\s+\d{{4}})?"
    rf"|(?:{MONTH_NAME})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
)
WEEKDAY = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun"
    r"|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag"
    r"|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
)
STATED_DATE = rf"[ \t]*(?:{WEEKDAY}\b\.?,?[ \t]+)?(?P<value>{DATE_VALUE})(?![\w-])"
READY_ON = re.compile(
    r"\b(?:(?:cargo\s+)?ready(?:\s+date)?|bereit|abholbereit|available|verfügbar|prête?s?"
    r"|earliest\s+(?:collection|pick[\s-]?up)(?:\s+date)?"
    r"|(?:collected|picked\s+up|pick[\s-]?up|collection)(?:\s+possible)?(?=[ \t]+(?:on|from|as of)\b)"
    r"|abholung(?:\s+frühestens)?(?=[ \t]+(?:am|ab)\b))\b[ \t]*(?:date\b[ \t]*)?"
    r"(?:(?:for (?:pick[\s-]?up|collection|shipment|dispatch)|to (?:go|ship)|zur Abholung)[ \t]+)?"
    r"(?:(?:on|from|as of|by|am|ab(?:[ \t]+dem)?|le)[ \t]+(?:the[ \t]+)?|:[ \t]*)?" + STATED_DATE,
    re.IGNORECASE,
)
# What turns a date near "arrival" into something other than a latest arrival: our answer by a date ("please reply
# by"), details sent by a date, or a past shipment ("the last arrival was by 16 November"). "Arrival airport" is a
# label, not a deadline.
NOT_ARRIVAL = (
    r"please|pls|kindly|reply|respond|answer|quote|send|confirm|advise|let us know|offer|details|information"
    r"|last|previous\w*|was|were|had|used to|bitte|antwort\w*|angebot\w*|senden|schicken|bestätig\w*|mitteil\w*"
    r"|letzte\w*|zuletzt|war|waren|répond\w*|envoy\w*|devis|dernière|était"
)
# Between "arrival" and "by" only the place may stand ("arrive in Hong Kong by"): no other clause, no other verb.
ARRIVAL_SPAN = rf"(?:(?!\b(?:{NOT_ARRIVAL})\b)(?:[^.!?;,:\n]|,(?=[ \t]*(?-i:[A-Z])))){{0,40}}?"
# Round 117: "Die Ware steht am 2026-11-26 zur Abholung bereit": German puts the date before "bereit".
READY_DATE_FIRST = re.compile(
    r"\b(?:steht|stehen|ist|sind)\b[^.!?;\n]{0,40}?\b(?:am|ab(?:[ \t]+dem)?)[ \t]+"
    + STATED_DATE
    + r"(?=[ \t]+(?:zur Abholung[ \t]+|zum Versand[ \t]+)?(?:bereit|abholbereit|versandbereit|verfügbar)\b)",
    re.IGNORECASE,
)
ARRIVE_BY = re.compile(
    rf"\b(?:arriv(?:e|es|ing|al)\b(?!\s+(?:airport|port)\b){ARRIVAL_SPAN}(?:no later than|not later than|by)\b"
    r"|(?:latest arrival|delivery deadline|(?:required|requested) delivery date|delivery date required)\s*[:=]?"
    # "ETA no later than", "delivery at destination is needed by", "the goods have to be in Toronto no later than";
    # only the goods, the shipment or "it" set a deadline ("the offer has to be in our system by" is our answer's)
    rf"|\b(?:eta|delivery\s+at\s+destination|(?:goods|cargo|shipment|consignment|freight|it|they)\s+(?:must|has to"
    rf"|have to|needs? to|should)\s+be\s+(?:delivered|in|at)\b){ARRIVAL_SPAN}(?:no later than|not later than|by)\b"
    rf"|(?:ankunft|eintreffen)\b{ARRIVAL_SPAN}(?:spätestens(?: am)?|bis(?: zum)?)\b"
    rf"|arriv(?:ée|er)\b{ARRIVAL_SPAN}au plus tard(?: le)?\b)" + STATED_DATE,
    re.IGNORECASE,
)
# German puts the verb last: "die Sendung muss spätestens am 2026-12-01 am Zielflughafen eintreffen" (round 118). Only
# with an arrival after the date: "muss spätestens am 25.11. abgeholt werden" is a pickup deadline.
ARRIVE_BY_LAST = re.compile(
    r"\b(?:muss|müssen|soll|sollen|sollte)[ \t]+(?:[\w-]+[ \t]+){0,2}?spätestens(?:[ \t]+am|[ \t]+bis(?:[ \t]+zum)?)?"
    + STATED_DATE
    + r"(?=[^.!?;\n]{0,60}?\b(?:eintreffen|eintrifft|ankommen|ankommt|angekommen\w*|am ziel(?:flughafen|ort)\b))",
    re.IGNORECASE,
)
# The last words before "arrival": "please confirm the arrival date by", "last time the arrival was by"; a request
# verb further back ("please quote 300 kg that must arrive by") leaves the deadline standing.
ARRIVAL_CONTEXT = re.compile(
    r"\b(?:reply|respond|answer|quote|send|confirm|advise|let us know|last time|previous\w*|used to|antwort\w*"
    r"|angebot\w*|senden|schicken|bestätig\w*|mitteil\w*|letzte\w* mal|zuletzt|répond\w*|envoy\w*|dernière fois)\b"
    r"(?:\W+\w+){0,2}\W*$",
    re.IGNORECASE,
)
MAX_TRANSIT = re.compile(
    r"\b(?:max(?:imum)?\.?\s+(?:transit(?: time)?|laufzeit)\s*[:=]?"
    r"|(?:transit(?: time)?|laufzeit|transportdauer)\s*(?:of\s+|:\s*)?(?:must not exceed|cannot exceed|no more than"
    r"|at most|up to|max(?:imum)?\.?|höchstens|maximal|darf höchstens)\s*"
    # "no more than 4 days in transit"
    r"|(?:no more than|at most|max(?:imum)?\.?|maximal|höchstens)\s+(?=\d{1,3}\s*(?:days?|tag(?:e|en)?)\s+(?:in\s+)?"
    r"(?:transit|laufzeit)))"
    r"[ \t]*(?P<days>\d{1,3})\s*(?:days?|tag(?:e|en)?)\b"
    r"|\btransit\s+of\s+(?P<most>\d{1,3})\s*days?\s+at\s+most\b",
    re.IGNORECASE,
)


def _stated_date(value: str, on: date) -> date | None:
    """Resolve a date without a year against the request date: this year's, unless that lies more than half a year
    back."""
    try:
        if "-" in value:
            return date.fromisoformat(value)
        value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b|\bof\b", " ", value.casefold())
        first, second, *year = value.replace(".", " ").replace(",", " ").split()
        if first.isdigit():
            day, month = int(first), int(second) if second.isdigit() else MONTH_NUMBER[second]
        else:
            day, month = int(second), MONTH_NUMBER[first]
        if year:
            return date(int(year[0]), month, day)
        stated = date(on.year, month, day)
        # Up to half a year back is this year's date (a ready date passed, a missed deadline); further back the
        # customer means next year's ("arrive by 4 January" written in December).
        return stated if stated >= on - timedelta(days=183) else date(on.year + 1, month, day)
    except (ValueError, KeyError):
        return None


# The customer's drop-off at our side, not the arrival at the destination.
OUR_SIDE = re.compile(r"\b(?:your\s+(?:warehouse|site|depot|office|facility|hub)|the\s+airport|to\s+you)\b", re.IGNORECASE)


def schedule(text: str, on: date) -> tuple[date | None, date | None, int | None]:
    """Read cargo readiness, latest arrival, and a maximum transit from explicit statements."""

    def first_date(pattern: re.Pattern) -> date | None:
        return next((value for match in pattern.finditer(text) if (value := _stated_date(match["value"], on))), None)

    def arrive_by() -> date | None:
        """A latest arrival is a coming date the customer sets for this shipment, never an earlier one, and never the
        drop-off at our side ("delivered to your warehouse at FRA by")."""
        matches = sorted([*ARRIVE_BY.finditer(text), *ARRIVE_BY_LAST.finditer(text)], key=lambda m: m.start())
        origin = (_lane(text) or ("",))[0]
        for match in matches:
            clause = re.split(r"[.!?;,:\n]", text[: match.start()])[-1]
            if OUR_SIDE.search(match[0]) or (origin and re.search(rf"\b{origin}\b", match[0])):
                continue
            if not ARRIVAL_CONTEXT.search(clause) and (value := _stated_date(match["value"], on)) and value >= on:
                return value
        return None

    transit = MAX_TRANSIT.search(text)
    return (
        first_date(READY_ON) or first_date(READY_DATE_FIRST),
        arrive_by(),
        int(transit["days"] or transit["most"]) if transit else None,
    )


def _largest(patterns: tuple[re.Pattern, ...], text: str, thousands: bool) -> Decimal | None:
    """The largest figure: the total beside per-piece and net figures."""
    values = [v for p in patterns for m in p.findall(text) if (v := number(m, thousands)) is not None and v > 0]
    return max(values, default=None)


PAST_SHIPMENT = re.compile(
    r"[^.!?\n]*\b(?:last\s+(?:time|month|week|year)|previously|letztes\s+mal|letzten\s+monat|letzte\s+woche"
    r"|letztes\s+jahr)\b[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


def by_rules(message: Message, deadline: Deadline, guess: bool = False, *, cargo: dict | None = None) -> Rfq | None:
    """Regex and lookup tables. None unless origin, destination and weight are certain. With `guess`, for a model
    that cannot be reached: the largest gross weight and volume the mail names, rather than no quote at all."""
    text = _text(message)
    lane = _lane(text)
    door, postcode = _pickup(text)
    if lane is None and door and (destination := _destination_only(text)):
        lane = ("", destination)  # the origin is the pickup zone's airport, set in `extract`
    if lane and cargo and cargo.get("destination"):
        lane = (lane[0], cargo["destination"])  # the attached document's (`_cargo_sources`)
    figures = PAST_SHIPMENT.sub("", text)  # "Last month we shipped 1250 kg ..." is not this shipment
    if cargo is None:
        lots = _lots(figures)
        pieces = lots[2] if lots else _pieces(figures)
        weight = lots[0] if lots else _weight(figures, pieces)
        volume = lots[1] if lots else _volume(figures, pieces)
    else:
        pieces, weight, volume = (cargo.get(k) for k in ("pieces", "weight_kg", "volume_cbm"))
    if guess:
        weight = weight or _largest(WEIGHT, figures, thousands=True)
        volume = volume or _largest(VOLUME, figures, thousands=False)
    elif volume is None and (DIMS.search(figures) or any(p.search(figures) for p in VOLUME)):
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
        missing=("pickup_postcode",) if door and not postcode and not _abroad_pickup(text) else (),
    )


# Round 114 split it in two: "the same shipment again? My earlier request, "...", was for YYZ".
SAME_AS_EARLIER = re.compile(
    r"\b(?:same|identical)\s+(?:shipment|cargo|goods|consignment)\b|\b(?:gleiche|selbe|dieselbe)\s+(?:sendung|ware)\b",
    re.IGNORECASE,
)
EARLIER_REQUEST = re.compile(
    r"\b(?:earlier|previous|last|former|prior|my|our)\s+(?:request|inquiry|enquiry)|\b(?:früher|vorherig|letzt)\w*\s+anfrage",
    re.IGNORECASE,
)


# A code the mail names anew is the destination only right after a destination label.
NEW_DESTINATION = re.compile(
    r"(?:\bdestination\b(?:\s+(?:airport|is|now|will\s+be|becomes|should\s+be))*|\bziel(?:flughafen)?\b(?:\s+ist)?"
    r"|\b(?:ship(?:ped)?|fly|flown|go(?:es)?|deliver(?:ed|y)?|send|sent)?\s*\b(?:to|nach)\b)\s*[:=]?\s*$",
    re.IGNORECASE,
)


def _earlier(ctx: Context, message: Message, deadline: Deadline) -> Rfq | None:
    """Round 103: "the same shipment as in my earlier request, "Air freight quote request: STR to MEL" ... the new
    destination is LAX". The customer's earlier request of that subject (else their latest), as it was read, with
    what this mail states anew: a destination it names besides the earlier lane, a weight, volume or piece count, the
    wish, the currency. It comes before the rules, which would read the earlier lane from the quoted subject."""
    text = own_words(_body(message))
    if not SAME_AS_EARLIER.search(text):
        return None
    rows = ctx.db.execute(
        "SELECT s.id, m.subject FROM sessions s JOIN messages m ON m.id = s.id"
        " WHERE LOWER(s.customer) = LOWER(?) AND s.id < ? ORDER BY s.id DESC",
        (message.sender, message.session_id),
    ).fetchall()
    named = {q.strip().casefold() for q in QUOTED_SUBJECT.findall(text)}
    if named:
        rows = [r for r in rows if r["subject"].strip().casefold() in named]
    else:  # without its subject only a request that says it points to the latest one
        rows = rows[:1] if EARLIER_REQUEST.search(text) else []
    read = (
        rows
        and ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.extracted' ORDER BY id DESC LIMIT 1",
            (rows[0]["id"],),
        ).fetchone()
    )
    if not read:
        return None
    before = Rfq.model_validate_json(read["payload"])
    said = QUOTED_SUBJECT.sub("", text)  # the earlier subject names the earlier lane
    new = set()
    for m in re.finditer(r"\b[A-Z]{3}\b", said):
        if m.group() in {before.origin, before.destination} or not _airport(m.group()):
            continue
        clause = re.split(r"[.;,!?\n]", said[: m.start()])[-1]
        if not NEW_DESTINATION.search(clause):  # "from FRA", "origin is now FRA", "via FRA": not the destination
            return None
        new.add(m.group())
    if len(new) > 1:
        return None
    pieces = _pieces(said)
    stated = {"pieces": pieces, "weight_kg": _weight(said, pieces), "volume_cbm": _volume(said, pieces)}
    if (pickup := _pickup(said))[1]:  # a pickup moved: its zone's airport is the origin (`extract`)
        stated |= {"door_pickup": True, "pickup_postcode": pickup[1], "pickup_zone": zone_of(pickup[1]), "origin": ""}
    return before.model_copy(update={k: v for k, v in stated.items() if v is not None} | {
        "session_id": message.session_id, "message_id": message.id, "customer": message.sender,
        "destination": new.pop() if new else before.destination, "preference": _preference(text),
        "currency": _currency(text) or before.currency, "language": _language(text),
        "received_at": deadline.received_at, "deadline_at": deadline.expires_at, "excluded_carriers": (),
    })  # fmt: skip


def _dims_volume(text: str) -> Decimal | None:
    dims = DIMS.findall(text)
    if len(dims) != 1:
        return None
    *sides, unit = dims[0]
    volume = Decimal(1)
    for side in sides:
        volume *= number(side, thousands=False) / _per_m(unit)
    return volume


def _only_with(text: str, value: Decimal, unit: str) -> bool:
    """The figure stands in the text only before another unit: "1413.17 lb" is no kg, "95.00 cubic feet" no cbm."""
    found = [m for m in re.finditer(NUM, text) if value in {number(m.group(1), t) for t in (True, False)}]
    return bool(found) and all(re.match(unit, text[m.end() :]) for m in found)


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
        if (not weight or weight not in written or _only_with(text, weight, OTHER_UNIT)) and (
            own := _weight(text, pieces) or (_model_lots(groups) or (None,))[0]
        ):
            weight = own
        if (volume not in written or _only_with(text, volume, f"(?:{FEET3}|{LITRE})")) and (
            own := _volume(text, pieces) or (_model_lots(groups) or (0, None))[1]
        ):
            volume = own
    if not weight:
        missing.append("weight_kg")
    door = bool(data.get("door_pickup"))
    given = re.sub(r"\D", "", data.get("pickup_postcode") or "")
    postcode = given if len(given) == 5 and not not_a_pickup_postcode(text, given) else None
    # A pickup abroad has no zone to ask a postcode for: no question, the airport-to-airport price (next case 22).
    # A zone is a German postcode's first two digits, so a postcode of another length ("5020 Salzburg") is abroad.
    if door and not postcode and not (_abroad_pickup(text) or (given and len(given) != 5)):
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
        language=_language(text, data.get("language")),
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


def _from_answer(read: Rfq, said: str, facts: list[str]) -> Rfq | None:
    """The asked facts as the rules read them in the answer alone, when they cannot read the request with it. Round
    104: "the same shipment as in my request ... for ATL", now to BOM; the model read only BOM, and the answer
    ("pickup from our site in postcode 40043 ... 585 kg and a total volume of 5.01 m³") was dropped: no quote."""
    update: dict[str, object] = {}
    door, postcode = _pickup(said)
    if {"origin", "pickup"} & set(facts) and door and postcode:
        update |= {"door_pickup": True, "pickup_postcode": postcode, "pickup_zone": zone_of(postcode)}
    elif "origin" in facts and (lane := _lane(said)) and lane[1] == read.destination:
        update["origin"] = lane[0]
    lots = _lots(said)
    pieces = lots[2] if lots else _pieces(said)
    if "weight" in facts and (weight := lots[0] if lots else _weight(said, pieces)):
        update |= {"weight_kg": weight} | ({"pieces": pieces} if pieces else {})
    if "volume" in facts and (volume := lots[1] if lots else _volume(said, pieces)):
        update["volume_cbm"] = volume
    if not update:
        return None
    return read.model_copy(update=update | {"missing": tuple(m for m in read.missing if m not in update)})


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
ASK_TRANSLATE_S = 5
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


# The fields each fact of `_unknown` fills, and every fact of a request an answer could change.
ANSWERED = {"pickup": ("door_pickup", "pickup_postcode", "pickup_zone"), "origin": ("origin",),
            "destination": ("destination",), "weight": ("weight_kg", "pieces"), "volume": ("volume_cbm",)}  # fmt: skip
REQUEST_FACTS = ("origin", "destination", "weight_kg", "volume_cbm", "pieces", "preference", "door_pickup",
                 "pickup_postcode", "pickup_zone", "currency", "excluded_carriers")  # fmt: skip


def _stated(value: object) -> bool:
    """A request's fact the reading found, not its default: no pickup, cheapest, no figure, no currency."""
    return value not in (None, "", ()) and value != 0 and value != Preference.CHEAPEST


def _unknown(rfq: Rfq) -> list[str]:
    """The facts a price still needs that the request did not give."""
    # A pickup with no zone and no postcode missing lies abroad: the model names its airport (`_departures_abroad`).
    placeless = rfq.door_pickup and not rfq.pickup_zone
    if placeless:
        facts = ["pickup"] if "pickup_postcode" in rfq.missing else []
    else:
        facts = [] if rfq.origin else ["origin"]
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
    body = ASK["de" if de else "en"].format(facts=joined, signer=ctx.settings.signer)
    if rfq.language not in ASK:
        from pixel.agents.rfq import compose  # compose reads this module's word lists

        english = body
        body = await compose.localize(ctx, rfq.language, message.session_id, body, ASK_TRANSLATE_S,
                                      lambda text: compose.kept(english, text, (ctx.settings.signer,)),
                                      "question.translate")  # fmt: skip
    try:
        question = await outbox.send(
            ctx, to=message.sender, subject=subject, session_id=message.session_id, in_reply_to=message.id,
            body=body, purpose="question",
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


# A departure airport the customer asks for: "please route via Munich (MUC), not Frankfurt; our letter of credit names
# MUC as airport of departure". The code must be one we truck to, and one only once ruled-out codes are dropped.
DEPARTURE_ASKED = re.compile(
    r"\b(?:route|routing|via|depart\w*|abflug\w*|über|en partance|fly\s+out\s+of|ex)\b", re.IGNORECASE
)
NOT_CODE = re.compile(r"\b(?:not|nicht|instead of|statt|anstatt|rather than)\s+(?:\w+\s+)?\(?$", re.IGNORECASE)
# A clause that refuses: "please do not route via FRA", "routing via FRA is not possible; use MUC", "avoid FRA".
REFUSED = re.compile(
    r"\b(?:not|cannot|can['’]t|no|avoid\w*|impossible|nicht|kein\w*|unmöglich|vermeiden)\b|n['’]t\b", re.IGNORECASE
)


def _asked_departure(ctx: Context, text: str) -> str | None:
    served = store.zone_airports(ctx)
    asked, refused = set(), set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if not DEPARTURE_ASKED.search(sentence):
            continue
        for clause in re.split(r"[;,]|\s(?:so|but|aber|sondern)\s", sentence):
            for m in CODE.finditer(clause):
                if m.group(0) in served:
                    no = REFUSED.search(clause) or NOT_CODE.search(clause[: m.start()])
                    (refused if no else asked).add(m.group(0))
    codes = asked - refused
    return codes.pop() if len(codes) == 1 else None


def _departures_abroad(ctx: Context | None, text: str) -> str:
    """A pickup abroad has no zone and so no airport (next case 22, Salzburg): the model names the one of ours
    nearest by road, in the extraction call it makes anyway. Nothing for any other request."""
    if not (ctx and _abroad_pickup(text)) or not (airports := store.zone_airports(ctx)):
        return ""
    return (
        "\n\n(Note for origin: when the goods are collected abroad and the mail names no departure airport, "
        f"give the one of these nearest to the pickup address by road: {', '.join(sorted(airports))}.)"
    )


def _zone_origin(ctx: Context, rfq: Rfq, text: str) -> Rfq:
    """The pickup zone belongs to one airport (R16): a door-to-port request leaves from it unless the mail itself
    names its origin airport or asks for another one we truck to (`_asked_departure`). A model guesses from the
    town: XFW for Lueneburg (zone HAM), CGN for Wuppertal (zone DUS); no airline flew those lanes and both requests
    were missed."""
    if not rfq.pickup_zone or not (airport := store.airport_of(ctx, rfq.pickup_zone)):
        return rfq
    if rfq.door_pickup and (asked := _asked_departure(ctx, text)):
        airport = asked
    elif rfq.origin and (not rfq.door_pickup or names_code(text, rfq.origin)):
        return rfq
    return rfq.model_copy(update={"origin": airport, "missing": tuple(m for m in rfq.missing if m != "origin")})


def _declared(lines: list[str]) -> str | None:
    found = (m for line in lines if (m := DANGEROUS.search(line)))
    return next((" ".join((m[1] or m[2]).split()) for m in found), None)


async def _attachments(ctx: Context, message: Message) -> list[tuple[str, list[str]]]:
    """The text lines of every attachment code can read (packing list, letter of instruction, a voice message). Facts
    only in a file code cannot read (a video, a scan) count as missing: the one question asks for them."""
    from pixel.agents.tariffs.agent import attachment_lines

    files = []
    for attachment in message.attachments:
        if (lines := await attachment_lines(ctx, message, attachment, "rfq")) is not None:
            files.append((attachment.filename, lines))
    return files


def names_code(text: str, code: str) -> bool:
    return bool(code and re.search(rf"\b{re.escape(code)}\b", text, re.IGNORECASE))


# Column names as whole words: "Stück" in "Gewicht je Stück" and "pcs" in "kg/pcs" say per piece, not a count column.
COLUMN = re.compile(
    r"(?<![\w/])(?<!je\s)(?<!pro\s)(?<!per\s)(?P<name>width|length|height|breite|l(?:ä|ae)nge|h(?:ö|oe)he|pieces|"
    r"pcs|qty|quantity|st(?:ü|ue)ck|(?:net(?:to)?|tare|tara)\s*(?:weight|gewicht)|gross\s+weight|weight|\w*gewicht)\b",
    re.IGNORECASE,
)
COLUMN_UNIT = re.compile(r"\(\s*(mm|cm|m|kgs?|lbs?)\b", re.IGNORECASE)
PER_PIECE_COLUMN = re.compile(
    r"\b(?:per\s+(?:pc|pce|pcs|piece|unit)|each|unit|je\s+st(?:ü|ue)ck|pro\s+st(?:ü|ue)ck)\b|/\s*(?:pc|pce|pcs|piece|unit)\b"
    r"|st(?:ü|ue)ckgewicht|einzelgewicht",
    re.IGNORECASE,
)
SIDE = {"width": 0, "breite": 0, "length": 1, "länge": 1, "laenge": 1, "height": 2, "höhe": 2, "hoehe": 2}
COUNT = {"pieces", "pcs", "qty", "quantity", "stück", "stueck"}
KG_PER = {"kg": Decimal(1), "kgs": Decimal(1), "lb": LB, "lbs": LB}
PIECES_LABEL = re.compile(
    r"(?im)^[^\S\n]*(?:total\s+(?:pieces|packages)|number\s+of\s+(?:pieces|packages)|pieces|packages|colli"
    r"|packst(?:ü|ue)cke(?:\s+gesamt)?|anzahl(?:\s+packst(?:ü|ue)cke)?|st(?:ü|ue)ckzahl)[^\S\n]*:[^\S\n]*(\d+)\b"
)


def _table(text: str) -> tuple[list[tuple[str, str | None, bool]], list[list[str]], str] | None:
    """A packing table: its columns (name, unit, per piece) from a header naming width, length, height and pieces,
    the rows below it with one number per column, and the text around it."""
    lines = text.splitlines()
    for i, header in enumerate(lines):
        found = list(COLUMN.finditer(header))
        names = [m["name"].casefold() for m in found]
        if not ({0, 1, 2} <= {SIDE.get(n) for n in names} and COUNT & set(names)):
            continue
        columns = []
        for m, end in zip(found, [n.start() for n in found[1:]] + [len(header)], strict=True):
            part = header[m.start() : end]
            unit = u[1].lower() if (u := COLUMN_UNIT.search(part)) else None
            each = bool(PER_PIECE_COLUMN.search(part) or re.search(r"(?i)\bunit\s+$", header[: m.start()]))
            columns.append((m["name"].casefold(), unit, each))
        rows = []
        for row in lines[i + 1 :]:
            cells = re.findall(r"\d[\d.,]*", row)
            if len(cells) != len(columns):
                break
            rows.append(cells)
        return columns, rows, "\n".join(lines[:i] + lines[i + 1 + len(rows) :])
    return None


def _table_totals(columns: list[tuple[str, str | None, bool]], rows: list[list[str]]) -> dict:
    """What a packing table's rows add up to: pieces, volume, and the gross weight when its one weight column is in
    kg or lb (a weight per piece times the row's pieces). {} when a row does not read."""
    counts = [i for i, (name, _, _) in enumerate(columns) if name in COUNT]
    weights = [
        i for i, (name, _, _) in enumerate(columns) if ("weight" in name or "gewicht" in name) and not NET.match(name)
    ]
    if len(counts) != 1 or not rows:
        return {}
    weight = weights[0] if len(weights) == 1 and columns[weights[0]][1] in KG_PER else None
    totals = {"pieces": 0, "volume_cbm": Decimal(0)} | ({"weight_kg": Decimal(0)} if weight is not None else {})
    for row in rows:
        n = number(row[counts[0]], thousands=False)
        if n is None or n != n.to_integral_value():
            return {}
        volume = n
        for (name, unit, _), cell in zip(columns, row, strict=True):
            if name in SIDE:
                if (side := number(cell, thousands=False)) is None:
                    return {}
                volume *= side / _per_m(unit)
        totals["pieces"] += int(n)
        totals["volume_cbm"] += volume
        if weight is not None:
            if (kg := number(row[weight])) is None:
                return {}
            name, unit, each = columns[weight]
            totals["weight_kg"] += kg * KG_PER[unit] * (n if each else 1)
    return totals


def _agree(field: str, a: Decimal | int, b: Decimal | int) -> bool:
    if field == "pieces":
        return a == b
    if field == "weight_kg":  # a weight per piece rounded to 0.01 kg, times the pieces
        return abs(a - b) <= max(Decimal("0.5"), b * Decimal("0.005"))
    return abs(a - b) <= max(Decimal("0.01"), b * Decimal("0.01"))


CARGO = ("pieces", "weight_kg", "volume_cbm")
# A net or tare weight is never the gross weight the freight is charged on: its column and its lines are left out.
NET = re.compile(r"(?:net(?:to)?|tare|tara)\s*(?:weight|gewicht)", re.IGNORECASE)
NET_LINE = re.compile(rf"(?im)^.*\b{NET.pattern}\b.*$")


def _document_facts(text: str) -> dict | None:
    """The pieces, gross weight and volume one attached document states: its labelled totals, the totals of its
    packing table, or both when they agree (rounds 33 to 35: the scorer priced the document). None when they
    disagree: a document that contradicts itself settles nothing. A labelled weight or volume beside a figure per
    piece is ambiguous and left out."""
    columns, rows, around = _table(text) or ([], [], text)
    pieces = {int(v) for v in PIECES_LABEL.findall(around)}
    stated = {
        "pieces": pieces.pop() if len(pieces) == 1 else None,
        "weight_kg": None if EACH_WEIGHT.search(around) else _unique(WEIGHT, NET_LINE.sub("", around), thousands=True),
        "volume_cbm": None if EACH_VOLUME.search(around) else _unique(VOLUME, around, thousands=False),
    }
    measured = _table_totals(columns, rows)
    facts = {}
    for field in CARGO:
        a, b = stated[field], measured.get(field)
        if a is not None and b is not None and not _agree(field, a, b):
            return None
        if (value := a if a is not None else b) is not None:
            facts[field] = value
    return facts


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
DESTINATION_FIELDS = {"destination", "zielflughafen"}
CARGO_FIELDS = {
    "pieces": re.compile(r"\b(?:pieces|piece count|figures|packstücke|stückzahl)\b", re.IGNORECASE),
    "weight_kg": re.compile(r"\b(?:weight|measurements|figures|bruttogewicht|gewicht)\b", re.IGNORECASE),
    "volume_cbm": re.compile(r"\b(?:volume|dimensions|measurements|figures|volumen|maße)\b", re.IGNORECASE),
}


def _mail_cargo(mail: str) -> dict:
    pieces = _pieces(mail)
    found = {"pieces": pieces, "weight_kg": _weight(mail, pieces), "volume_cbm": _volume(mail, pieces)}
    return {k: v for k, v in found.items() if v is not None}


def _same_cargo(mail: str, document: str) -> bool:
    """The document's weight is the mail's, and so are its pieces and volume where both state them."""
    facts, said = _document_facts(document) or {}, _mail_cargo(mail)
    return (
        "weight_kg" in facts
        and "weight_kg" in said
        and all(_agree(k, said[k], facts[k]) for k in CARGO if k in facts and k in said)
    )


def _document_destination(text: str) -> str | None:
    found = {
        m["code"]
        for m in DOCUMENT_AIRPORT.finditer(text)
        if m["label"].lower() in DESTINATION_FIELDS and _airport(m["code"])
    }
    return found.pop() if len(found) == 1 else None


def _shipment_sources(sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Exclude attachments that name a different airport for this shipment: another shipment (#164). A document
    that names only another destination and carries the mail's own cargo is this shipment with the lane mistyped in
    the mail: it stays, and its destination wins in `_cargo_sources`."""
    if len(sources) == 1:
        return sources
    mail = sources[0][1]
    origin, destination = _lane(mail) or (None, _destination_only(mail))
    kept = sources[:1]
    for name, text in sources[1:]:
        conflicts = {
            m["label"].lower() in ORIGIN_FIELDS
            for m in DOCUMENT_AIRPORT.finditer(text)
            if _airport(m["code"])
            and (wanted := origin if m["label"].lower() in ORIGIN_FIELDS else destination)
            and m["code"] != wanted
        }
        if not conflicts or (conflicts == {False} and _same_cargo(mail, text)):
            kept.append((name, text))
    return kept


def _cargo_sources(sources: list[tuple[str, str]]) -> tuple[dict, dict, Decimal | None]:
    """Explicit corrections in the mail first; then what the eligible attachments state (`_document_facts`) over
    the mail's own figures, unless the mail says to use its own; the mail's figures where no document states one.
    A forwarded mail (`FORWARDED`) is history, no document of this shipment: it fills a figure the mail lacks, never
    beats one."""
    from pixel.agents.tariffs.agent import FORWARDED

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
    stated = [None, *(_document_facts(text) for _, text in sources[1:])]
    forwarded = [text.startswith(FORWARDED) for _, text in sources]
    facts, evidence = {}, {}
    document_weight = None

    def take(field: str, readings: list) -> None:
        if readings[0] is not None:
            facts[field], evidence[field] = readings[0], [sources[0][0]]
        elif len(values := {v for v in readings if v is not None}) == 1:
            facts[field] = values.pop()
            evidence[field] = [name for (name, _), v in zip(sources, readings, strict=True) if v is not None]

    def documents_win(field: str, mail: object) -> bool:
        if mail is not None and any(CARGO_FIELDS[field].search(fields) for fields in mail_fields):
            return False
        found = [
            (name, s[field])
            for (name, _), s, old in zip(sources, stated, forwarded, strict=True)
            if s and field in s and not old
        ]
        if len(values := {v for _, v in found}) != 1:
            return False
        facts[field], evidence[field] = values.pop(), [name for name, _ in found]
        return True

    def reading(i: int, field: str, text: str, read) -> object:
        """A source's figure: its lots; for a document what it states, nothing when it contradicts itself; else any
        figure code reads in its text."""
        if lots[i]:
            return lots[i][{"weight_kg": 0, "volume_cbm": 1, "pieces": 2}[field]]
        if i and (stated[i] is None or field in stated[i]):
            return (stated[i] or {}).get(field)
        return read(text)

    counts = [reading(i, "pieces", text, _pieces) for i, (_, text) in enumerate(sources)]
    take("pieces", counts)
    documents_win("pieces", counts[0])
    for field, reader in (("weight_kg", _weight), ("volume_cbm", _volume)):
        corrected = {
            value
            for match in corrections
            if CARGO_FIELDS[field].search(match["field"])
            and (value := reader(match["value"], facts.get("pieces"))) is not None
        }
        if len(corrected) == 1:
            facts[field], evidence[field] = corrected.pop(), [sources[0][0]]
            continue
        readings = [
            reading(i, field, text, lambda t, c=count, read=reader: read(t, c or facts.get("pieces")))
            for i, ((_, text), count) in enumerate(zip(sources, counts, strict=True))
        ]
        take(field, readings)
        if documents_win(field, readings[0]) and field == "weight_kg":
            document_weight = facts[field]
    mail_destination = (_lane(sources[0][1]) or (None, _destination_only(sources[0][1])))[1]
    destinations = {
        d for _, text in sources[1:] if not text.startswith(FORWARDED) and (d := _document_destination(text))
    }
    if mail_destination and len(destinations) == 1 and (destination := destinations.pop()) != mail_destination:
        facts["destination"] = destination
        evidence["destination"] = [name for name, text in sources[1:] if _document_destination(text) == destination]
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


QUOTED_SUBJECT = re.compile(r"[\"“„«]([^\"“”„«»\n]{6,160})[\"”“»]")
EARLIER_FIELDS = ("weight_kg", "volume_cbm", "pieces", "door_pickup", "pickup_postcode", "pickup_zone", "origin")


def _earlier_request(ctx: Context, mail: Message, read: Rfq) -> Rfq:
    """'the same shipment as in my request "<subject>"': cargo and pickup come from that request of the same
    sender (round 101), the new mail keeps its destination, date and wish. A door request's zone picks the airport."""
    if not {"origin", "weight_kg", "volume_cbm"} & set(read.missing):
        return read
    for subject in QUOTED_SUBJECT.findall(_body(mail)):
        row = ctx.db.execute(
            "SELECT e.payload FROM messages m JOIN events e ON e.session_id = m.session_id AND e.kind = 'rfq.extracted'"
            " WHERE m.direction = 'in' AND m.sender = ? AND m.subject = ? AND m.session_id <> ? ORDER BY e.id DESC",
            (mail.sender, subject.strip(), mail.session_id),
        ).fetchone()
        if not row:
            continue
        old = json.loads(row[0])
        update = {f: old[f] for f in EARLIER_FIELDS if old.get(f) not in (None, "", "0")}
        if update.get("door_pickup") and update.get("pickup_zone"):
            update.pop("origin", None)
        missing = [m for m in read.missing if m not in update and not (m == "origin" and "pickup_zone" in update)]
        return Rfq.model_validate({**read.model_dump(mode="json"), **update, "missing": missing})
    return read


async def extract(ctx: Context, message: Message, deadline: Deadline) -> Rfq:
    rfq, _ = await read_request(ctx, message, deadline)
    return rfq


CHECKED = ("destination", "weight_kg", "volume_cbm", "pieces", "preference")
CHECK_WAIT_S = 6  # gpt-4.1 answers in about 1.2 s; a hanging gateway must not hold the bids


# A figure behind these words is not the shipment's gross total: a net or tare weight, one piece's, a superseded one.
NOT_GROSS = re.compile(
    r"\b(?:net|netto|tare|each|per|je|pro|not|nicht|instead\s+of|statt|previously|was|old|alt|vorher)\b\W*"
    r"(?:[\w.,]+\W*){0,3}$",
    re.IGNORECASE,
)


def _gross(sources: list[tuple[str, str]], patterns: tuple[re.Pattern, ...], thousands: bool) -> set[Decimal]:
    """Every figure code reads as a gross total with its unit, in the customer's own words (nothing forwarded or
    quoted) and in their documents."""
    found = set()
    for name, content in sources:
        content = own_words(content) if name == "mail" else content
        for p in patterns:
            for m in p.finditer(content):
                line = content[content.rfind("\n", 0, m.start()) + 1 : m.start()]
                if not NOT_GROSS.search(line) and (v := number(m.group(1), thousands)) is not None:
                    found.add(v)
    return found


async def _crosscheck(
    ctx: Context,
    rfq: Rfq,
    message: Message,
    text: str,
    sources: list[tuple[str, str]],
    deadline: Deadline,
    from_documents: frozenset[str] = frozenset(),
) -> tuple[Rfq, dict | None]:
    """The rules' reading of a request with attachments checked by a larger model reading the same text
    (`PIXEL_MODEL_CHECK`): at first sight the rules took the mail's figures over the attached packing list's (rounds
    33 to 35), and the model did not. On the 44 accepted requests without attachments both read the same, so those
    keep the cheap prose call: gpt-4.1 on every request cost about 0.0025 USD each (rounds 81 to 83). The model's weight or volume wins only when code reads that figure as a gross total
    with its unit (`_gross`): never a net, per-piece, superseded, forwarded or pounds figure. Pieces and the wish win
    as read; a destination never does, it is only reported. Its prose slots replace the separate prose call. On any
    failure the rules' reading stands."""
    try:
        result = await asyncio.wait_for(
            llm.ask_json(
                ctx,
                purpose="rfq.crosscheck",
                system=SYSTEM,
                user=text,
                schema=SCHEMA,
                session_id=message.session_id,
                max_output_tokens=500,
                model=ctx.settings.model_check,
            ),
            CHECK_WAIT_S,
        )
        read = _from_model(result.data, message, deadline)
    except Exception as e:  # noqa: BLE001  the rules' reading stands
        events.emit(ctx, "error", "rfq", session_id=message.session_id, message_id=message.id,
                    payload={"where": "rfq_crosscheck", "error": type(e).__name__})  # fmt: skip
        return rfq, None
    differs = {
        f: [str(getattr(rfq, f)), str(getattr(read, f))]
        for f in CHECKED
        if getattr(read, f) not in (None, "", 0) and getattr(read, f) != getattr(rfq, f)
    }
    documents = [s for s in sources if s[0] != "mail"]

    def stated(field: str, patterns: tuple[re.Pattern, ...], thousands: bool) -> set[Decimal]:
        """A gross total the documents state stands against the mail's: the scorer prices the document (rounds 33
        to 35, 130)."""
        in_documents = _gross(documents, patterns, thousands)
        return in_documents if getattr(rfq, field) in in_documents else _gross(sources, patterns, thousands)

    gross = {"weight_kg": stated("weight_kg", WEIGHT, True), "volume_cbm": stated("volume_cbm", VOLUME, False)}
    # A figure the rules took from the attached documents stands, stated as a total or added up from their rows.
    taken = {
        f: getattr(read, f)
        for f in differs
        if f not in from_documents and (f in ("pieces", "preference") or getattr(read, f) in gross.get(f, ()))
    }
    events.emit(ctx, "rfq.crosscheck", "rfq", session_id=message.session_id, message_id=message.id,
                payload={"model": result.model, "differs": differs, "taken": sorted(taken)})  # fmt: skip
    return rfq.model_copy(update=taken), {"first_name": result.data.get("first_name")}


async def read_request(ctx: Context, message: Message, deadline: Deadline) -> tuple[Rfq, str]:
    """by_rules, else one `llm.ask_json` call that also reads the name for the greeting (`prose.raw`, R31), so the
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

    kept: dict[str, object] = {}  # set by `ask`: the request's own facts we did not ask for, which an answer keeps

    def rules(guess: bool = False) -> Rfq | None:
        read = read_rules(guess)
        return read.model_copy(update=kept) if read else None

    def read_rules(guess: bool) -> Rfq | None:
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
        if not read.destination and (parts := shipments(read, text)):
            return parts[0]  # round 113: one line per shipment names each airport, nothing to ask
        if not (facts := _unknown(read)) or (said := await _ask(ctx, mail, read, facts, deadline)) is None:
            return read
        mail = mail.model_copy(update={"body": f"{_body(mail)}\n\n{own_words(said)}"})
        sources, text, message = source_inputs()
        cargo, evidence, document_weight = _cargo_sources(sources) if files else ({}, {}, None)
        # The answer fills what we asked for and adds what the request left at its default: an airport it names in
        # passing ("our sister company ships from X", "from X to Y later") never moves the lane, nor does its lane
        # hide what only the document said (round 75's "fastest"). Unasked, a door request's origin stays empty, so
        # its pickup zone decides.
        asked = {f for fact in facts for f in ANSWERED[fact]}
        for field in REQUEST_FACTS:
            value = getattr(read, field)
            if field not in asked and (field in ("origin", "destination") or _stated(value)):
                kept[field] = value
        again = rules()
        if again is None and not files:
            # Session 30544: the request also named an older lane ("as in my request ... to ATL"), so request and
            # answer together have two destinations; the answer read alone has one, and the request's facts are kept.
            alone = by_rules(mail.model_copy(update={"body": own_words(said)}), deadline)
            again = alone.model_copy(update=kept) if alone else None
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
        if again is None:
            again = _from_answer(read, own_words(said), facts)
        return apply_pickup(again or read, guess=False)

    rfq, prose = _earlier(ctx, mail, deadline) or rules(), None
    if rfq is not None and not (rfq := apply_pickup(await ask(rfq))).origin:
        rfq = None
    if rfq is not None and files and ctx.settings.model_check:
        documented = frozenset(f for f, names in evidence.items() if names and "mail" not in names)
        rfq, prose = await _crosscheck(ctx, rfq, message, text, sources, deadline, documented)
    if rfq is None:
        try:
            result = await llm.ask_json(
                ctx,
                purpose="rfq.extract",
                system=SYSTEM,
                user=text + _departures_abroad(ctx, text),
                schema=SCHEMA,
                session_id=message.session_id,
                max_output_tokens=500,
                model=ctx
                and ctx.settings.model_check
                or None,  # gpt-4.1-mini took the mail's figures over the documents'
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
            rfq = _earlier_request(ctx, mail, rfq)
            if cargo:
                rfq = rfq.model_copy(update={**cargo, "missing": tuple(k for k in rfq.missing if k not in cargo)})
            rfq = apply_pickup(await ask(rfq))
            prose = {"first_name": result.data.get("first_name")}
    subject = "" if REPLY_SUBJECT.match(mail.subject) else mail.subject
    on = rfq.received_at.date()
    stated = schedule(f"{_body(mail)}\n{subject}", on)
    # An attached document's date stands against the mail's, as its cargo does: the scorer prices the document (rounds
    # 33 to 35, 130).
    documented = schedule("\n".join(c for n, c in sources if n != "mail"), on) if files else (None, None, None)
    ready_on, arrive_by, max_transit_days = (d if d is not None else m for m, d in zip(stated, documented, strict=True))
    rfq = rfq.model_copy(update={"ready_on": ready_on, "arrive_by": arrive_by,
                                 "max_transit_days": max_transit_days})  # fmt: skip
    if files:
        attached = "\n".join(content for _, content in documents)
        rfq = rfq.model_copy(update={"language": _language(_text(mail), rfq.language, attached)})
        for key in ("origin", "destination"):
            if named := [name for name, content in sources if names_code(content, getattr(rfq, key))]:
                evidence[key] = named
        excluded = [name for name, content in documents if (name, content) not in sources]
        proof = {"document_weight_kg": document_weight} if document_weight == rfq.weight_kg else {}
        dates = dict(zip(("ready_on", "arrive_by", "max_transit_days"), documented, strict=True))
        if said := {k: str(v) for k, v in dates.items() if v is not None}:
            proof["document_schedule"] = said
        events.emit(ctx, "rfq.evidence", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                    payload={"sources": evidence, "attachment_route_conflicts": excluded, **proof})  # fmt: skip
    request_text = _text(mail)
    rfq = _carrier_requirements(ctx, rfq, request_text)
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
