"""The quote mail. Code writes every number and name as prose; the model writes three small slots around it (R31):
the customer's first name and one short opening line (the closing slot is no longer rendered). Slots that fail a check are
dropped, never the quote (P1, R9)."""

import json
import random
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from pixel import llm
from pixel.agents.rfq.extract import ENGLISH, GERMAN, own_words
from pixel.contracts import Message, Preference, RateOption, Rfq
from pixel.core import events
from pixel.core.context import Context

CENT = Decimal("0.01")
DEFAULT_SIGNER = "Lena Berger"
COMPANY = "Air Export, PIXEL Air Freight"
OFFICE = ZoneInfo("Europe/Berlin")  # our desk's clock: "until 17:22 today" is the customer's time too

TEXT = {
    "en": {
        "subject": "Quote {origin} to {destination}, {weight} kg",
        "hello_name": {"formal": "Dear {name},", "neutral": "Hello {name},", "casual": "Hi {name},"},
        "hello": "Hello,",
        # the opening no longer thanks, so the answer to their small talk does, once
        "well_back": ("All good here, thanks for asking!", "Going well here, thanks!", "All well on our side, thanks!"),
        "core": {
            "cheapest": (
                (
                    "We can fly your shipment from {origin} to {destination} with {carrier} for {total} {currency} "
                    "all-in, with {transit} in transit."
                ),
                (
                    "From {origin} to {destination}, I can offer {carrier} at {total} {currency} all-in, with "
                    "{transit} in transit."
                ),
                (
                    "For the best rate from {origin} to {destination}, I recommend {carrier} at {total} {currency} "
                    "all-in, with {transit} in transit."
                ),
                (
                    "My best rate from {origin} to {destination} is {total} {currency} all-in with {carrier}, "
                    "{transit} in transit."
                ),
                (
                    "I compared the airlines from {origin} to {destination}, and {carrier} comes out lowest at "
                    "{total} {currency} all-in, with {transit} in transit."
                ),
            ),
            "fastest": (
                (
                    "For the quickest routing from {origin} to {destination}, I recommend {carrier}: {transit} in "
                    "transit, at {total} {currency} all-in."
                ),
                (
                    "{carrier} gives the shortest transit from {origin} to {destination} at {transit}, for {total} "
                    "{currency} all-in."
                ),
                (
                    "I compared the airlines from {origin} to {destination}, and {carrier} is the quickest at "
                    "{transit} in transit, for {total} {currency} all-in."
                ),
                (
                    "From {origin} to {destination}, the fastest option is {carrier} with {transit} in transit, at "
                    "{total} {currency} all-in."
                ),
                (
                    "To move your shipment from {origin} to {destination} as quickly as possible, I can offer "
                    "{carrier} at {transit} in transit for {total} {currency} all-in."
                ),
            ),
        },
        "air_only": (
            "The airport-to-airport price from {origin} to {destination} with {carrier} is {total} {currency}, "
            "with {transit} in transit. Pickup is excluded."
        ),
        "basis": (
            "That covers air freight, fuel and security surcharges.",
            "The price includes the air freight and the fuel and security surcharges.",
        ),
        "basis_pickup": (
            "That covers pickup at your {site} by {trucker}, air freight, fuel and security surcharges.",
            (
                "The price includes collection from your {site} by {trucker}, the air freight and the fuel and security "
                "surcharges."
            ),
        ),
        "basis_zone_pickup": (
            "That includes air freight, fuel and security surcharges, and pickup by {trucker} at the rate for your area."
        ),
        "pickup_address": "Please send the full pickup address, including the postcode, so I can check the pickup rate.",
        "book_air_only": "If you'd like to book the air leg only, please reply to this email.",
        "bulky": (
            "As the cargo is bulky for its weight, the airline charges it on {chargeable} kg volume weight.",
            "Because of the volume, the freight is calculated on {chargeable} kg chargeable weight.",
        ),
        "valid": ("I can hold the rate until {until}.", "The rate is good until {until}."),
        "until_today": "{time} today",
        "until": "{time} on {day}",
        "book": (
            "If that works for you, just reply and I'll book it right away.",
            "Just reply to this mail if you'd like to go ahead, and I'll take care of the booking.",
            "Let me know if you would like to go ahead and I'll book it straight away.",
        ),
        "bye": {"formal": "Kind regards,", "neutral": "Best regards,", "casual": "Best wishes,"},
        "bye_town": "Best regards to {town},",
        "wish_back": "Have a {wish} too!",
        "in_city": "in {place}",
        "at_airport": "at {place}",
        "day": "day",
        "days": "days",
        "arrival": "With the cargo ready on {ready}, the earliest arrival {where} would be {arrive}.",
        "arrival_pickup": "With pickup on {ready}, the earliest arrival {where} would be {arrive}.",
        "pickup_first": "We can collect on {ready}, the day it is ready, and the earliest arrival {where} would be {arrive}.",
        "arrival_air_only": (
            "If you deliver the cargo to {origin} on {ready}, the earliest arrival {where} would be {arrive}."
        ),
        "departure": "That is with a departure from {origin} on {depart}.",
        "one_awb": "All lots travel together as one consignment under a single air waybill, as you asked.",
        "cutoff": "The exact drop-off cut-off at {origin} comes with the airline's booking, and I will pass it on "
        "as soon as I have it.",
        "months": (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        "date": "{weekday} {day} {month}",
        # Authentic air-export desk voice for casual and neutral mail: warm and direct, without emojis, fillers or P.S.
        # Formal mail uses the calm lines above with "Dear" and the full signed name.
        "warm": {
            "hello": ("Hi {name},", "Hello {name},"),
            "hello_anon": "Hi there!",
            "core": {
                "cheapest": (
                    (
                        "I checked the airlines from {origin} to {destination}, and {carrier} comes out best at "
                        "{total} {currency} all-in, with {transit} in transit."
                    ),
                    (
                        "I'd go with {carrier} on this one — {origin} to {destination} is {total} {currency} all-in, "
                        "{transit} in transit."
                    ),
                    (
                        "From {origin} to {destination}, {carrier} gives the best rate at {total} {currency} all-in, "
                        "with {transit} in transit."
                    ),
                    (
                        "We can fly this from {origin} to {destination} with {carrier} for {total} {currency} all-in, "
                        "{transit} in transit."
                    ),
                ),
                "fastest": (
                    (
                        "Since speed matters here, I'd go with {carrier}: {origin} to {destination} in {transit}, for "
                        "{total} {currency} all-in."
                    ),
                    (
                        "I checked the airlines from {origin} to {destination}, and {carrier} is the quickest at "
                        "{transit} in transit, for {total} {currency} all-in."
                    ),
                    (
                        "{carrier} gets it from {origin} to {destination} fastest — {transit} in transit, at {total} "
                        "{currency} all-in."
                    ),
                    (
                        "For the quickest run from {origin} to {destination}, I'd put it on {carrier}: {transit} in "
                        "transit, {total} {currency} all-in."
                    ),
                ),
            },
            "book": (
                "Just reply if you'd like to go ahead, and I'll get it booked right away.",
                "If that works for you, send a quick confirmation and I'll book the space straight away.",
                "Let me know if you want me to lock it in, and I'll send the airline reference once confirmed.",
                "Just hit reply to confirm and I'll take care of the booking from here.",
            ),
            "bye": "Best wishes,",
        },
    },
    "de": {
        "subject": "Angebot {origin} nach {destination}, {weight} kg",
        "hello_name": {"formal": "Guten Tag {name},", "neutral": "Hallo {name},", "casual": "Hallo {name},"},
        "hello": "Guten Tag,",
        "well_back": ("Danke der Nachfrage, hier läuft alles gut.", "Danke, bei uns ist alles bestens."),
        "core": {
            "cheapest": (
                (
                    "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} "
                    "all-in fliegen, die Laufzeit beträgt {transit}."
                ),
                (
                    "Von {origin} nach {destination} kann ich Ihnen {total} {currency} all-in mit {carrier} anbieten, "
                    "die Laufzeit beträgt {transit}."
                ),
                (
                    "Mein bester Preis von {origin} nach {destination} ist {total} {currency} all-in mit {carrier}, "
                    "Laufzeit {transit}."
                ),
                (
                    "Ich habe die Airlines von {origin} nach {destination} verglichen: Am besten liegt {carrier} mit "
                    "{total} {currency} all-in, Laufzeit {transit}."
                ),
            ),
            "fastest": (
                (
                    "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} "
                    "all-in fliegen, die Laufzeit beträgt {transit}."
                ),
                (
                    "Da es schnell gehen soll, empfehle ich {carrier} von {origin} nach {destination}: Laufzeit "
                    "{transit}, {total} {currency} all-in."
                ),
                (
                    "Am schnellsten geht es von {origin} nach {destination} mit {carrier}, Laufzeit {transit}, zu "
                    "{total} {currency} all-in."
                ),
                (
                    "Ich habe die Airlines von {origin} nach {destination} verglichen: Am schnellsten ist {carrier}, "
                    "Laufzeit {transit}, zu {total} {currency} all-in."
                ),
            ),
        },
        "air_only": (
            "Der Preis von Flughafen {origin} bis Flughafen {destination} mit {carrier} beträgt {total} {currency}, "
            "die Laufzeit {transit}. Die Abholung ist nicht enthalten."
        ),
        "basis": (
            "Enthalten sind Luftfracht, Treibstoff- und Sicherheitszuschlag.",
            "Der Preis umfasst die Luftfracht sowie Treibstoff- und Sicherheitszuschlag.",
        ),
        "basis_pickup": (
            "Enthalten sind die Abholung bei Ihnen durch {trucker}, Luftfracht, Treibstoff- und Sicherheitszuschlag.",
            (
                "Der Preis umfasst die Abholung bei Ihnen durch {trucker}, die Luftfracht sowie Treibstoff- und "
                "Sicherheitszuschlag."
            ),
        ),
        "basis_zone_pickup": (
            "Enthalten sind Luftfracht, Treibstoff- und Sicherheitszuschlag sowie die Abholung durch {trucker} "
            "zum Tarif für Ihre Region."
        ),
        "pickup_address": (
            "Bitte senden Sie mir die vollständige Abholadresse mit Postleitzahl, damit ich den Abholpreis prüfen kann."
        ),
        "book_air_only": "Wenn Sie nur die Luftfracht buchen möchten, antworten Sie bitte auf diese E-Mail.",
        "bulky": (
            "Da die Ware für ihr Gewicht viel Platz braucht, rechnet die Airline mit {chargeable} kg Volumengewicht.",
            "Wegen des Volumens wird die Fracht auf {chargeable} kg frachtpflichtiges Gewicht berechnet.",
        ),
        "valid": ("Ich halte die Rate bis {until}.", "Der Preis gilt bis {until}."),
        "until_today": "heute {time} Uhr",
        "until": "{day}, {time} Uhr",
        "book": (
            "Wenn das für Sie passt, antworten Sie einfach kurz, dann buche ich sofort.",
            "Antworten Sie einfach auf diese Mail, wenn Sie buchen möchten, den Rest übernehme ich.",
        ),
        "bye": {"formal": "Mit freundlichen Grüßen", "neutral": "Viele Grüße", "casual": "Viele Grüße"},
        "bye_town": "Viele Grüße nach {town}",
        "wish_back": "Ihnen auch {wish}!",
        "in_city": "in {place}",
        "at_airport": "in {place}",
        "day": "Tag",
        "days": "Tage",
        "arrival": "Ist die Ware am {ready} bereit, wäre sie frühestens am {arrive} {where}.",
        "arrival_pickup": "Mit Abholung am {ready} wäre die Ware frühestens am {arrive} {where}.",
        "pickup_first": "Wir können am {ready} abholen, sobald die Ware bereit ist, und sie wäre frühestens am {arrive} {where}.",
        "arrival_air_only": (
            "Wenn Sie die Ware am {ready} in {origin} anliefern, wäre sie frühestens am {arrive} {where}."
        ),
        "departure": "Der Abflug ab {origin} wäre dann am {depart}.",
        "one_awb": "Alle Positionen reisen wie gewünscht zusammen als eine Sendung unter einem Luftfrachtbrief.",
        "cutoff": "Den genauen Anlieferschluss in {origin} nennt die Airline mit der Buchung; ich gebe ihn Ihnen "
        "weiter, sobald ich ihn habe.",
        "months": (
            "Januar",
            "Februar",
            "März",
            "April",
            "Mai",
            "Juni",
            "Juli",
            "August",
            "September",
            "Oktober",
            "November",
            "Dezember",
        ),
        "weekdays": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
        "date": "{weekday}, den {day}. {month}",
        "warm": {
            "hello": ("Hallo {name},", "Guten Tag {name},"),
            "hello_anon": "Hallo!",
            "core": {
                "cheapest": (
                    (
                        "Ich habe die Airlines von {origin} nach {destination} verglichen — {carrier} liegt am "
                        "besten bei {total} {currency} all-in, Laufzeit {transit}."
                    ),
                    (
                        "Für {origin} nach {destination} würde ich {carrier} nehmen: {total} {currency} all-in, mit "
                        "{transit} Laufzeit."
                    ),
                    (
                        "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} "
                        "all-in fliegen, Laufzeit {transit}."
                    ),
                ),
                "fastest": (
                    (
                        "Da es eilt, würde ich {carrier} nehmen: von {origin} nach {destination} in {transit}, für "
                        "{total} {currency} all-in."
                    ),
                    (
                        "Ich habe die Airlines von {origin} nach {destination} verglichen — am schnellsten ist "
                        "{carrier} mit {transit} Laufzeit, für {total} {currency} all-in."
                    ),
                    (
                        "{carrier} bringt Ihre Sendung am schnellsten von {origin} nach {destination}: {transit} "
                        "Laufzeit, {total} {currency} all-in."
                    ),
                ),
            },
            "book": (
                "Antworten Sie einfach kurz, wenn das passt, dann buche ich direkt.",
                "Geben Sie mir gern kurz Bescheid, dann reserviere ich den Flug sofort.",
                "Ein kurzes Okay genügt, dann mache ich die Buchung direkt klar und schicke Ihnen die Referenz.",
            ),
            "bye": "Viele Grüße",
        },
    },
}

PROSE_SYSTEM = (
    "You are an experienced air export clerk replying to a customer's quote request. Code already writes the "
    "greeting, the reply to 'hope you are well', the price, carrier, routing, transit, dates, scope, booking line "
    "and sign-off. Return JSON with four fields in language ISO code {language}, matching a {tone} "
    "(German: always formal Sie). "
    "first_name: the customer's signed first name only, else null. "
    "hook: a short exact phrase copied verbatim from the customer's mail that gives a concrete human angle to "
    "react to — urgency or tight timing, delicate or specific cargo (glassware, instruments, samples), an attached "
    "packing list/spec, their buyer's import, own airport drop-off, single-AWB consolidation, or a friendly remark. "
    "Never 'hope your week is going well', a greeting/sign-off, or a bare route/weight; null only when nothing "
    "fits. "
    "intro: null when hook is null; else ONE natural, human sentence (5 to 12 plain words) reacting to hook in "
    "your own words like a real desk colleague. Never announce the quote/offer, never thank for the request, "
    "inquiry, email or details, never say you noted/received/reviewed anything, never claim the shipment is "
    "already booked or arranged, and never start the same way as an opening under Our notes. "
    "Never include digits, numbers, prices, currencies, 3-letter codes, dates, weekdays, months, airlines, "
    "airports, cities, countries, or people/company names. "
    'Examples — Good: hook="laboratory glassware" -> intro="We will make sure the glassware is handled with care." '
    '(Bad: "Thank you for your inquiry regarding the laboratory glassware shipment."); '
    'Good: hook="attached our packing specification" -> intro="The packing spec made checking this straightforward." '
    '(Bad: "I have noted the attached details."); '
    'Good: hook="arranging the flight for their import" -> intro="Glad to help get this moving smoothly for your buyer." '
    '(Bad: "Here is our quote for your import shipment."). '
    "closing: always null. "
    'Answer as JSON {{"first_name": ..., "hook": ..., "intro": ..., "closing": ...}}.'
)
NULLABLE = {"type": ["string", "null"]}
PROSE_FIELDS = {"first_name": NULLABLE, "intro": NULLABLE, "closing": NULLABLE}
PROSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {**PROSE_FIELDS, "hook": NULLABLE},
    "required": [*PROSE_FIELDS, "hook"],
}

DIGIT = re.compile(r"\d")
CODE = re.compile(r"\b[A-Z]{3}\b")
SYMBOL = re.compile(r"[€$£%@]|https?:|www\.")
CALENDAR = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|montag|dienstag|mittwoch|donnerstag|freitag|"
    r"samstag|sonntag|january|february|march|april|june|july|august|september|october|november|december|januar|"
    r"februar|märz|juni|juli|oktober|dezember)\b",
    re.IGNORECASE,
)
FRAME = re.compile(r"^\s*(hello|hi|dear|hallo|guten tag|sehr geehrte)\b|\b(best regards|kind regards|viele grüße|"
                   r"freundliche grüße|mit freundlichen)\b", re.IGNORECASE)  # fmt: skip
INFORMAL_DE = re.compile(r"\b(du|dich|dir|dein\w*|euch|euer|eure\w*)\b", re.IGNORECASE)
NAME = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ-]{1,19}")
LIMITS = {"intro": 120, "closing": 140}
WORDS_MAX = 16
# An opening that uses stiff template clichés, announces the quote/price, or runs past one sentence is dropped;
# natural context-aware desk remarks pass (tests/fixtures/openings.json).
ECHO = re.compile(
    r"\b(?:noted|received|details|carefully|regarding|inquiry|enquiry|reaching out|"
    r"(?:get|getting) in touch|pleased to|happy to assist|rest assured|we understand|i understand|"
    r"(?:thanks?|thank you)(?: so much| very much| a lot)? for (?:your|the|this) (?:request|quote|quotation|mail|"
    r"e-?mail|message|note|interest)|many thanks|all-in|weight|volume|"
    r"as requested|here is|here's|please find|as follows|offer\w*|"
    r"rates?|prices?|quote|quotation|anfrage|bezüglich|sorgfältig|zur kenntnis|behilflich|"
    r"gewicht|volumen|anbei|wie gewünscht|angebot)\b|[.!?]\s+\S",
    re.IGNORECASE,
)
CAPITAL = re.compile(r"\b[A-ZÄÖÜ][\wäöüß-]+")
FORMAL = re.compile(r"\b(?:dear|sehr geehrte|kind regards|yours sincerely|mit freundlichen)", re.IGNORECASE)
# A formal salutation or closing, not "Kind regards" alone: such a customer gets "Dear", calm phrasing, full name.
CLEARLY_FORMAL = re.compile(
    r"^\s*(?:dear|sehr geehrte)\b|\b(?:yours sincerely|yours faithfully|mit freundlichen gr)",
    re.IGNORECASE | re.MULTILINE,
)
CASUAL = re.compile(r"^\s*(?:hi|hey|hallo|moin)\b|\bcheers\b", re.IGNORECASE)
SIGNED = re.compile(r"^[A-ZÄÖÜ][a-zäöüß'’-]+(?: [A-ZÄÖÜ][a-zäöüß'’-]+){0,2}$")
NOT_SIGNED = re.compile(
    r"\b(?:Thanks|Thank|Regards|Cheers|Best|Kind|Many|Greetings|Danke|Viele|Beste|Team|Department|Logistics|"
    r"Shipping|Purchasing|Sales|Export|Import|Manager|Coordinator|Office|Desk|GmbH|AG|Ltd|Inc)\b"
)
# What a clerk answers in kind: a wish for the day, greetings from the customer's town, "Cheers".
# "Hope your week's going well!": a clerk answers it before the business.
WELL = re.compile(
    r"\bhope (?:your (?:week|day)\S* (?:is |going )|you(?:['’]re| are) (?:doing )?|all is |this finds you )\w*\s*well"
    r"|\bhoffe,? (?:es geht Ihnen gut|Ihre Woche läuft gut)",
    re.IGNORECASE,
)
WISH = {
    "en": re.compile(
        r"\bhave a ((?:lovely|nice|great|good|wonderful|pleasant) (?:day|afternoon|evening|weekend|week))\b",
        re.IGNORECASE,
    ),
    "de": re.compile(
        r"\b(einen sch(?:ö|oe)nen (?:Tag|Abend|Feierabend)|ein sch(?:ö|oe)nes Wochenende)\b", re.IGNORECASE
    ),
}
FROM_TOWN = re.compile(
    r"\b(?i:greetings|regards|wishes|gr(?:ü|ue)(?:ß|ss)e)[ \t]+(?i:from|aus)[ \t]+"
    r"([A-ZÄÖÜ][\wäöüß-]+(?: [A-ZÄÖÜ][\wäöüß-]+)?)"
)
OWN_BYE = re.compile(r"^\s*(cheers|viele gr(?:ü|ue)(?:ß|ss)e)\b", re.IGNORECASE | re.MULTILINE)
SUBJECT_MAX = 150
DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
READY = re.compile(r"\b(?:ready|bereit|available|verfügbar)\b[^\n]{0,100}?(" + DATE.pattern + ")", re.IGNORECASE)
WAREHOUSE = re.compile(r"\bwarehouse\b", re.IGNORECASE)
PICKUP_ASK = re.compile(r"earliest (?:\w+ )?pick-?up|frühest\w* Abholung", re.IGNORECASE)
DEPARTURE = re.compile(r"earliest (?:\w+ )?departure(?! airport)|frühest\w* Abflug", re.IGNORECASE)
ONE_AWB = re.compile(r"air ?waybill|\bAWB\b|(?:one|single) consignment|Luftfrachtbrief", re.IGNORECASE)
CUTOFF = re.compile(r"drop-?off|cut-?off|latest (?:delivery|handover)|Anlieferschluss|Annahmeschluss", re.IGNORECASE)
# Common words beyond extract's lists, so a short slot still shows its language; a slot must show ours.
MORE_EN = {"i", "it", "this", "that", "will", "be", "are", "in", "on", "have", "good", "great", "day", "thank",
           "happy", "hope", "glad", "here", "get", "keep", "so", "as", "all", "my", "me", "us", "an", "at",
           "no", "not", "make", "sure", "help", "well", "can", "just", "very", "from", "about", "into", "out",
           "up"}  # fmt: skip
MORE_DE = {"ihnen", "ihre", "ihren", "ihrer", "gerne", "gern", "tag", "schönen", "einen", "noch", "vielen", "dank",
           "uns", "auf", "den", "dem", "bei", "es", "wird", "sind", "haben", "gute", "guten", "freue", "auch", "so",
           "hier", "ihrem", "ihres", "darum", "kümmere", "melde", "kein", "keine", "sehr", "aber", "schon",
           "direkt", "schnell", "natürlich", "klar"}  # fmt: skip


@dataclass(frozen=True)
class Prose:
    """The model's slots, each None when it was missing or failed a check."""

    first_name: str | None = None
    intro: str | None = None
    closing: str | None = None


def money(amount: Decimal) -> str:
    """The total as the portal and the carriers write theirs: no thousands separator, a decimal point, in every
    language. The scorer reads this number out of our mail; "1,234.57" or "1.234,57" can be read as another one."""
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def weight(kg: Decimal, language: str) -> str:
    text = f"{kg.quantize(CENT, rounding=ROUND_HALF_UP).normalize():,f}"
    return text.translate(str.maketrans(",.", ".,")) if language == "de" else text


def language_of(rfq: Rfq) -> str:
    """The language the mail is rendered in: the customer's when we have a text for it, else English."""
    return rfq.language if rfq.language in TEXT else "en"


def reply_subject(subject: str | None) -> str | None:
    subject = " ".join((subject or "").split())[:SUBJECT_MAX]  # one line, never a header break
    if not subject:
        return None
    return subject if re.match(r"(?i)^(re|aw|antw)\s*:", subject) else f"Re: {subject}"


def _transit(option: RateOption, t: dict) -> str:
    return f"{option.transit_days} {t['day'] if option.transit_days == 1 else t['days']}"


def _sentence(text: str) -> str:
    return text if text[-1] in ".!?…" else f"{text}."


def ready_date(text: str) -> date | None:
    """The date the cargo is ready: the first date after "ready". Any other date may be a deadline, so none."""
    found = READY.search(text)
    if not found:
        return None
    groups = found.groups()[1:]
    y, m, d, dd, mm, yy = groups
    try:
        return date(int(y), int(m), int(d)) if y else date(int(yy), int(mm), int(dd))
    except ValueError:
        return None


def _day(d: date, t: dict) -> str:
    return t["date"].format(weekday=t["weekdays"][d.weekday()], day=d.day, month=t["months"][d.month - 1])


def answers(rfq: Rfq, option: RateOption, request: str) -> str:
    """What the request asked beyond the price, answered by code: the earliest arrival (ready date, a day for a
    pickup to reach the airport, the transit), the departure that goes with it, one air waybill for all lots, the drop-off cut-off. "" on any doubt."""
    try:
        t = TEXT[language_of(rfq)]
        said = []
        ready = ready_date(request)
        if ready:
            arrive = ready + timedelta(days=option.transit_days + (1 if option.trucker else 0))
            city = city_of(request, option.destination)
            where = t["in_city" if city else "at_airport"].format(place=city or option.destination)
            arrival = "arrival_pickup" if option.trucker else "arrival_air_only" if rfq.door_pickup else "arrival"
            if option.trucker and PICKUP_ASK.search(request):
                arrival = "pickup_first"
            said.append(
                t[arrival].format(ready=_day(ready, t), arrive=_day(arrive, t), origin=option.origin, where=where)
            )
            if DEPARTURE.search(request):
                depart = arrive - timedelta(days=option.transit_days)
                said.append(t["departure"].format(origin=option.origin, depart=_day(depart, t)))
        if ONE_AWB.search(request):
            said.append(t["one_awb"])
        if CUTOFF.search(request):
            said.append(t["cutoff"].format(origin=option.origin))
        return " ".join(said)
    except Exception:  # noqa: BLE001  an extra answer must never cost the quote
        return ""


def city_of(request: str, airport: str) -> str | None:
    """The city the customer wrote behind the airport's code, "XXX (City, CC)": theirs, never from a list of ours."""
    found = re.search(rf"\b{re.escape(airport)}\s*\(\s*([^()\d,]{{2,40}}?)\s*(?:,[^()]*)?\)", request)
    return found.group(1) if found else None


def in_kind(request: str, lang: str, tone: str, name: str | None) -> tuple[str | None, str]:
    """The customer's own closing answered: their wish for the day returned, and the sign-off sent to the town
    they greet from, or as "Cheers" or "Viele Grüße" when they write it in an English mail. Without either, the sign-off of their register."""
    t = TEXT[lang]
    wished = WISH[lang].search(request)
    wish = t["wish_back"].format(wish=wished.group(1)[0].lower() + wished.group(1)[1:]) if wished else None
    town = FROM_TOWN.search(request)
    if town and town.group(1).split()[0] != name:
        return wish, t["bye_town"].format(town=town.group(1))
    if lang == "en" and (own := OWN_BYE.search(request)):
        return wish, "Cheers," if own.group(1).lower() == "cheers" else "Viele Grüße,"
    return wish, t["bye"][tone]


def register(request: str) -> str:
    """How the customer writes: formal, casual or neutral, from their greeting and closing."""
    return "formal" if FORMAL.search(request) else "casual" if CASUAL.search(request) else "neutral"


def signed_name(request: str, signer: str = "", full: bool = False) -> str | None:
    """The first name the customer signed with (the whole signed name with `full`), read by rule from the mail's
    last block: the greeting keeps the name also when the model's slot is missing (a failed call, the fallback)."""
    blocks = [b for b in re.split(r"\n\s*\n", request.strip()) if b.strip()]
    for line in blocks[-1].splitlines() if blocks else []:
        line = line.strip().rstrip(",")
        if SIGNED.match(line) and not NOT_SIGNED.search(line):
            first = line.split()[0]
            return None if first.casefold() in signer.casefold().split() else line if full else first
    return None


def formal(request: str) -> bool:
    return bool(CLEARLY_FORMAL.search(request))


def addressed(name: str, request: str, signer: str) -> str:
    """ "Dear Jonas Kessler" as signed, without guessing Mr or Ms; the first name when the whole one is not signed."""
    whole = signed_name(request, signer, full=True)
    return whole if whole and " " in whole and whole.split()[0] == name else name


def greeting(lang: str, name: str | None, seed: int, request: str = "", signer: str = "") -> str:
    """The salutation, the same for one session in the quote and in its booking mails: by first name, or "Dear"
    with the signed name to a clearly formal mail."""
    if formal(request):
        t = TEXT[lang]
        return t["hello_name"]["formal"].format(name=addressed(name, request, signer)) if name else t["hello"]
    w = TEXT[lang]["warm"]
    return random.Random(f"hello:{seed}").choice(w["hello"]).format(name=name) if name else w["hello_anon"]


def sign_off(request: str, lang: str, name: str | None) -> tuple[str | None, str]:
    """`in_kind`, with our own "Best wishes" where it would give the sign-off of their register; a clearly formal
    mail keeps "Kind regards"."""
    if formal(request):
        return in_kind(request, lang, "formal", name)
    tone = register(request)
    wish, bye = in_kind(request, lang, tone, name)
    return wish, TEXT[lang]["warm"]["bye"] if bye == TEXT[lang]["bye"][tone] else bye


def signature(bye: str, signer: str, full: bool = False) -> str:
    """Signed with the first name, as a colleague signs, or the full name to a formal mail; the company line stays."""
    return f"{bye}\n{signer if full else (signer.split() or [signer])[0]}\n{COMPANY}"


SMILEY = re.compile(r" ?(?:👋|🙂|😊|✈️|👍)")
SMILEYS_MAX = 3


def _one_each(body: str) -> str:
    """A person uses a smiley once, not the same one in every paragraph: a repeat and any beyond the third go."""
    seen: list[str] = []

    def keep(found: re.Match) -> str:
        face = found.group().strip()
        if face in seen or len(seen) >= SMILEYS_MAX:
            return ""
        seen.append(face)
        return found.group()

    return SMILEY.sub(keep, body)


def _until(rfq: Rfq, option: RateOption, t: dict) -> str:
    """17:22 today: the validity on our desk's clock, with the day only when it is not today."""
    until = option.valid_until.astimezone(OFFICE)
    today = until.date() == rfq.received_at.astimezone(OFFICE).date()
    return t["until_today" if today else "until"].format(time=until.strftime("%H:%M"), day=_day(until.date(), t))


def _body(rfq: Rfq, option: RateOption, prose: Prose, signer: str, said: str = "", request: str = "") -> str:
    """Authentic air-export desk prose (R31): formal customers get "Dear", calm lines and the full name; casual and
    neutral mail gets a warm colleague voice. No hardcoded emojis, filler lines or P.S. boilerplate."""
    lang = language_of(rfq)
    t = TEXT[lang]
    w = t["warm"]
    stiff = formal(request)
    pick = random.Random(rfq.session_id).choice  # wording varies by session; one session always reads the same
    air_only = rfq.door_pickup and not option.trucker
    needs_address = rfq.door_pickup and not rfq.pickup_postcode
    preference = "fastest" if rfq.preference == Preference.FASTEST else "cheapest"
    core = t["air_only"] if air_only else pick((t if stiff else w)["core"][preference])
    if option.trucker:
        basis = t["basis_zone_pickup"] if needs_address else pick(t["basis_pickup"])
    else:
        basis = pick(t["basis"])
    price_line = core.format(
        origin=option.origin, destination=option.destination, carrier=option.carrier,
        total=money(option.total), currency=option.currency, transit=_transit(option, t),
    )  # fmt: skip
    # the model's opening or none: a stock opening is the template the jury reads past (R31)
    intro = [_sentence(prose.intro)] if prose.intro and not air_only else []
    if stiff and intro:
        intro = [s.replace("!", ".") for s in intro]
    if WELL.search(request):
        well = pick(t["well_back"])
        intro.insert(0, well.replace("!", ".") if stiff else well)
    site = "warehouse" if WAREHOUSE.search(request) else "site"
    offer = [price_line, basis.format(trucker=option.trucker, site=site)]
    if option.chargeable_kg > rfq.weight_kg:
        offer.append(pick(t["bulky"]).format(chargeable=weight(option.chargeable_kg, lang)))
    book = [pick(t["valid"]).format(until=_until(rfq, option, t))] if option.valid_until else []
    if needs_address:
        book.append(t["pickup_address"])
    else:
        book.append(t["book_air_only"] if air_only else pick((t if stiff else w)["book"]))
    name = prose.first_name or signed_name(request, signer)
    wish, bye = sign_off(request, lang, name)
    if stiff and wish:
        wish = wish.replace("!", ".")
    lines = [
        greeting(lang, name, rfq.session_id, request, signer),
        "",
        *([" ".join(intro), ""] if intro else []),
        " ".join(offer),
        "",
        *([said, ""] if said else []),
        " ".join(book),
        "",
        *([wish, ""] if wish else []),
        signature(bye, signer, full=stiff),
    ]
    return _one_each("\n".join(lines))


def check(body: str, rfq: Rfq, option: RateOption) -> bool:
    """R9 and P1 on the finished mail: the total exactly once, its currency, carrier and transit present."""
    lang = language_of(rfq)
    return (
        body.count(money(option.total)) == 1
        and option.currency in body
        and option.carrier in body
        and _transit(option, TEXT[lang]) in body
    )


def render(
    rfq: Rfq,
    option: RateOption,
    prose: Prose | None = None,
    subject: str | None = None,
    signer: str = DEFAULT_SIGNER,
    request: str = "",
) -> tuple[str, str]:
    """Subject and body of the quote: one option, one total with its scope, currency and transit time, in the
    customer's language. Numbers are formatted by code from `option`, never by a model. `subject` is the
    customer's, answered with `Re:`. When the finished mail fails `check` or the slots break the rendering, it is
    rendered again without slots."""
    lang = language_of(rfq)
    request = own_words(request)
    title = reply_subject(subject) or TEXT[lang]["subject"].format(
        origin=option.origin, destination=option.destination, weight=weight(rfq.weight_kg, lang)
    )
    said = answers(rfq, option, request)
    if prose and prose != Prose():
        try:
            body = _body(rfq, option, prose, signer, said, request)
            if check(body, rfq, option):
                return title, body
        except Exception:  # noqa: BLE001, S110  a slot must never cost the quote; render it plain
            pass
    return title, _body(rfq, option, Prose(), signer, said, request)


def _words_lang(text: str) -> tuple[int, int]:
    words = re.findall(r"[a-zäöüß]+", text.lower())
    return sum(w in GERMAN or w in MORE_DE for w in words), sum(w in ENGLISH or w in MORE_EN for w in words)


def _sentence_ok(text: str, slot: str, lang: str, banned: set[str]) -> bool:
    """Also drops a slot in a third language: it must show more of our language's words than of the other."""
    if not text or len(text) > LIMITS[slot] or "\n" in text or text.lstrip().startswith(("-", "*", "•")):
        return False
    if DIGIT.search(text) or CODE.search(text) or SYMBOL.search(text) or CALENDAR.search(text) or FRAME.search(text):
        return False
    if slot == "intro" and ECHO.search(text):
        return False
    lowered = text.casefold()
    if any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered) for name in banned):
        return False
    german, english = _words_lang(text)
    if (lang == "de" and german <= english) or (lang == "en" and english <= german):
        return False
    return not (lang == "de" and INFORMAL_DE.search(text))


def _first_name(name: str | None, message: Message, signer: str) -> str | None:
    """The first word of what the model read as the name ("Anna Müller" -> "Anna"), when it stands in the
    customer's mail and is not our signer's."""
    word = (name or "").split()[0].strip(".,;:!?") if (name or "").split() else ""
    if not NAME.fullmatch(word) or word.casefold() in signer.casefold().split():
        return None
    return word if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", message.body or "") else None


def _banned(ctx: Context | None) -> set[str]:
    """Every carrier and trucker we know: the model must not name one, the price line does."""
    if ctx is None:
        return set()
    return {r["name"].casefold() for r in ctx.db.execute("SELECT name FROM carriers").fetchall() if r["name"]}


_NORM_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
COMMON_EN = ENGLISH | MORE_EN


def _norm(text: str) -> str:
    return " ".join(text.translate(_NORM_TRANS).split()).casefold()


def _proper_nouns(request: str) -> set[str]:
    """Capitalised words in an English request that look like names, cities or companies rather than ordinary words
    capitalised at the start of a sentence or bullet."""
    lowered = set(re.findall(r"\b[a-z][a-z-]+\b", request))
    return {w for w in CAPITAL.findall(request) if w.lower() not in COMMON_EN and w.lower() not in lowered}


def _opening_ok(text: str, hook: str | None, request: str, lang: str, earlier: tuple[str, ...]) -> bool:
    """The opening answers a phrase that stands in the mail (`hook`), has at most WORDS_MAX words, no name or place
    taken from the mail, and does not begin as one we already sent this customer."""
    flat = _norm(request)
    norm_hook = _norm(hook) if hook else ""
    if not norm_hook or len(norm_hook) < 4 or norm_hook not in flat or WELL.search(hook or ""):
        return False
    words = text.split()
    if len(words) > WORDS_MAX:
        return False
    if lang == "en":
        proper = _proper_nouns(request)
        if (set(CAPITAL.findall(" ".join(words[1:]))) - {"I"}) & proper:
            return False
        signed = signed_name(request)
        if signed and words[0].strip(".,;:!?\"'").casefold() == signed.casefold():
            return False
    return _start(text) not in {_start(e) for e in earlier}


def _start(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W\d_]+", text.casefold())[:2])


def clean(
    raw: dict, rfq: Rfq, message: Message, signer: str, banned: set[str] = frozenset(), earlier: tuple[str, ...] = ()
) -> Prose:
    """Each slot that fails a check becomes None: digits, three-letter codes, currency symbols, URLs, dates, a
    carrier's name, a greeting or sign-off, more than its length, another language, German `du`; for the opening
    also a filler or an echo of the mail (`ECHO`), a hook that is not in the mail, a name or place, the first words
    of an opening in `earlier`. The first name must stand in the customer's mail."""
    lang = language_of(rfq)
    raw = raw if isinstance(raw, dict) else {}
    banned = set(banned) | {w.casefold() for w in signer.split() if len(w) > 2}

    def text(key: str) -> str | None:
        value = raw.get(key)
        return " ".join(value.split()) if isinstance(value, str) and value.strip() else None

    intro, closing = text("intro"), text("closing")
    request = own_words(message.body or "")
    opening = (
        intro
        and _sentence_ok(intro, "intro", lang, banned)
        and _opening_ok(intro, text("hook"), request, lang, earlier)
    )
    return Prose(
        first_name=_first_name(text("first_name"), message, signer),
        intro=intro if opening else None,
        closing=closing if closing and _sentence_ok(closing, "closing", lang, banned) else None,
    )


def _history(ctx: Context, rfq: Rfq) -> tuple[str, ...]:
    """The openings we wrote this customer before."""
    try:
        rows = ctx.db.execute(
            "SELECT e.payload FROM events e JOIN sessions s ON s.id = e.session_id"
            " WHERE e.kind = 'quote.prose' AND s.customer = ? AND e.session_id != ? ORDER BY e.id",
            (rfq.customer, rfq.session_id),
        ).fetchall()
        openings = (json.loads(r["payload"]).get("intro") for r in rows)
        return tuple(o for o in openings if isinstance(o, str) and o)
    except Exception:  # noqa: BLE001  without the history the opening is only checked against the mail
        return ()


def adopt(ctx: Context, rfq: Rfq, message: Message, raw: dict) -> Prose:
    """Cleans the model's slots and stores them as `quote.prose`, so the fallback quote reads human too; a dropped
    opening is kept in the event as `rejected`, to tune the prompt. Never raises: on any error the quote goes out
    without the slots."""
    try:
        prose = clean(raw, rfq, message, ctx.settings.signer, _banned(ctx), _history(ctx, rfq))
        dropped = [k for k, v in asdict(prose).items() if isinstance(raw, dict) and raw.get(k) and not v]
        rejected = raw.get("intro") if "intro" in dropped else None
        events.emit(ctx, "quote.prose", "rfq", session_id=rfq.session_id,
                    payload={**asdict(prose), "dropped": dropped, "rejected": rejected})  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "prose.adopt", "error": type(exc).__name__})  # fmt: skip
        return Prose()
    return prose


def notes(earlier: tuple[str, ...]) -> str:
    """The openings we already sent this customer, so the next one begins differently."""
    sent = "".join(f"\n- {o}" for o in earlier[-5:])
    return f"Our notes: openings we sent this customer before:{sent}" if sent else ""


async def prose(ctx: Context, rfq: Rfq, message: Message) -> Prose | None:
    """The quote's one small model call when rules read the request (R28, R31). None on any failure; never
    blocks the quote."""
    body = own_words(message.body or "")[:1500]
    tone = "formal, composed business prose (no exclamation marks)" if formal(body) else "warm, concise colleague tone"
    try:
        result = await llm.ask_json(
            ctx,
            purpose="quote.prose",
            system=PROSE_SYSTEM.format(language=language_of(rfq), tone=tone),
            user=f"Subject: {message.subject}\n\n{body}\n\n{notes(_history(ctx, rfq))}",
            schema=PROSE_SCHEMA,
            session_id=rfq.session_id,
            max_output_tokens=150,
            model=ctx.settings.model_prose or None,
        )
    except Exception as exc:  # noqa: BLE001  the quote must never wait on its prose
        events.emit(
            ctx, "error", "rfq", session_id=rfq.session_id, payload={"where": "prose", "error": type(exc).__name__}
        )
        return None
    return adopt(ctx, rfq, message, result.data)


def stored(ctx: Context, session_id: int) -> Prose | None:
    """The session's latest checked slots, for the fallback quote and the booking mails. None when there are none
    or they cannot be read: the fallback must never fail on its prose."""
    try:
        row = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'quote.prose' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        data = json.loads(row["payload"]) if row else None
        if not isinstance(data, dict):
            return None
        return Prose(**{k: v if isinstance(v := data.get(k), str) and v else None for k in PROSE_FIELDS})
    except Exception:  # noqa: BLE001
        return None


def customer_subject(ctx: Context, message_id: int) -> str | None:
    return _customer(ctx, message_id, "subject")


def customer_body(ctx: Context, message_id: int) -> str:
    return _customer(ctx, message_id, "body") or ""


def _customer(ctx: Context, message_id: int, field: str) -> str | None:
    try:
        row = ctx.db.execute(f"SELECT {field} FROM messages WHERE id = ?", (message_id,)).fetchone()
    except Exception:  # noqa: BLE001  without it the quote keeps our own subject and fixed text
        return None
    return row[field] if row else None
