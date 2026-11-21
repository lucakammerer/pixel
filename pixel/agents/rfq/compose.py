import re
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

from pixel import llm
from pixel.contracts import Message, RateOption, Rfq
from pixel.core import events
from pixel.core.context import Context

CENT = Decimal("0.01")

TEXT = {
    "en": {
        "subject": "Quote {lane}, {weight} kg",
        "hello": "Hello,",
        "offer": "for your shipment {lane} ({chargeable} kg chargeable weight) we can offer:",
        "total": "{total} {currency} all-in with {carrier}, transit {transit}.",
        "included": "The price includes air freight, fuel and security surcharges.",
        "included_pickup": "The price includes pickup at your door by {trucker}, air freight, fuel and security "
        "surcharges.",
        "valid": "This offer is valid until {until} UTC.",
        "book": "Just reply to this mail and we will book it for you.",
        "bye": "Best regards\nPIXEL Air Freight",
        "day": "day",
        "days": "days",
    },
    "de": {
        "subject": "Angebot {lane}, {weight} kg",
        "hello": "Guten Tag,",
        "offer": "für Ihre Sendung {lane} ({chargeable} kg frachtpflichtiges Gewicht) können wir Ihnen anbieten:",
        "total": "{total} {currency} all-in mit {carrier}, Laufzeit {transit}.",
        "included": "Im Preis enthalten sind Luftfracht, Treibstoff- und Sicherheitszuschlag.",
        "included_pickup": "Im Preis enthalten sind die Abholung bei Ihnen durch {trucker}, Luftfracht, "
        "Treibstoff- und Sicherheitszuschlag.",
        "valid": "Dieses Angebot gilt bis {until} UTC.",
        "book": "Antworten Sie einfach auf diese Mail, und wir buchen die Sendung für Sie.",
        "bye": "Freundliche Grüße\nPIXEL Air Freight",
        "day": "Tag",
        "days": "Tage",
    },
}

OPENING_SYSTEM = (
    "You write the first sentence of a freight forwarder's reply to a customer who asked for an air freight "
    "quote. One short, warm, natural sentence in the language with ISO code {language}, referring to what the "
    "customer asked for. No numbers, prices, dates, airlines, airports or place names. Do not greet; the "
    'greeting is already written. Answer as JSON {{"sentence": "..."}}.'
)
OPENING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"sentence": {"type": "string"}},
    "required": ["sentence"],
}


def money(amount: Decimal) -> str:
    """The total as the portal and the carriers write theirs: no thousands separator, a decimal point, in every
    language. The scorer reads this number out of our mail; "1,234.57" or "1.234,57" can be read as another one."""
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def weight(kg: Decimal, language: str) -> str:
    text = f"{kg.quantize(CENT, rounding=ROUND_HALF_UP).normalize():,f}"
    return text.translate(str.maketrans(",.", ".,")) if language == "de" else text


def render(rfq: Rfq, option: RateOption, opening: str | None = None) -> tuple[str, str]:
    """Subject and body of the quote: one option, one all-in total with its currency, transit time,
    in the customer's language and tone. Numbers are formatted by code from `option`, never by a model."""
    lang = rfq.language if rfq.language in TEXT else "en"
    t = TEXT[lang]
    lane = f"{option.origin} → {option.destination}"
    transit = f"{option.transit_days} {t['day'] if option.transit_days == 1 else t['days']}"
    lines = [t["hello"], ""]
    if opening:
        lines += [opening, ""]
    lines += [
        t["offer"].format(lane=lane, chargeable=weight(option.chargeable_kg, lang)),
        "",
        t["total"].format(total=money(option.total), currency=option.currency, carrier=option.carrier, transit=transit),
        "",
        t["included_pickup"].format(trucker=option.trucker) if option.trucker else t["included"],
    ]
    if option.valid_until:
        lines.append(t["valid"].format(until=option.valid_until.astimezone(UTC).strftime("%d.%m.%Y %H:%M")))
    lines += ["", t["book"], "", t["bye"]]
    subject = t["subject"].format(lane=lane, weight=weight(rfq.weight_kg, lang))
    return subject, "\n".join(lines)


async def opening(ctx: Context, rfq: Rfq, message: Message) -> str | None:
    """One small model call for a human first sentence (R28, R31). None on any doubt; never blocks the quote."""
    try:
        result = await llm.ask_json(
            ctx,
            purpose="quote.opening",
            system=OPENING_SYSTEM.format(language=rfq.language),
            user=f"{message.subject}\n\n{message.body}"[:1500],
            schema=OPENING_SCHEMA,
            session_id=rfq.session_id,
            max_output_tokens=60,
        )
    except Exception as exc:  # noqa: BLE001  the quote must never wait on this sentence
        events.emit(
            ctx, "error", "rfq", session_id=rfq.session_id, payload={"where": "opening", "error": type(exc).__name__}
        )
        return None
    sentence = str(result.data.get("sentence") or "").strip()
    if not sentence or len(sentence) > 200 or re.search(r"\d|\b[A-Z]{3}\b", sentence):
        return None
    return sentence
