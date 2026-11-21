"""The quote mail. Code writes every number and name as prose; the model writes three small slots around it (R31):
the customer's first name, an intro that shows we read their mail, and a closing line. Slots that fail a check are
dropped, never the quote (P1, R9)."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

from pixel import llm
from pixel.agents.rfq.extract import ENGLISH, GERMAN
from pixel.contracts import Message, RateOption, Rfq
from pixel.core import events
from pixel.core.context import Context

CENT = Decimal("0.01")
DEFAULT_SIGNER = "Lena Berger"
COMPANY = "Air Export, PIXEL Air Freight"

TEXT = {
    "en": {
        "subject": "Quote {origin} to {destination}, {weight} kg",
        "hello_name": "Hello {name},",
        "hello": "Hello,",
        "intro": (
            "Thanks for your request, happy to help with this one.",
            "Thanks for reaching out, here is what we can do for you.",
        ),
        "core": (
            (
                "We can fly your shipment from {origin} to {destination} with {carrier} for {total} {currency} all-in, "
                "with {transit} in transit."
            ),
            (
                "Our best option from {origin} to {destination} is {carrier}: {total} {currency} all-in, "
                "{transit} in transit."
            ),
        ),
        "basis": "That is based on {chargeable} kg chargeable weight and covers air freight, fuel and security "
        "surcharges.",
        "basis_pickup": "That is based on {chargeable} kg chargeable weight and covers pickup at your door by "
        "{trucker}, air freight, fuel and security surcharges.",
        "valid": "The rate holds until {until} UTC.",
        "book": (
            "If that works for you, just reply and I'll book it right away.",
            "Just reply to this mail if you'd like to go ahead, and I'll take care of the booking.",
        ),
        "bye": "Best regards,\n{signer}\n" + COMPANY,
        "day": "day",
        "days": "days",
    },
    "de": {
        "subject": "Angebot {origin} nach {destination}, {weight} kg",
        "hello_name": "Hallo {name},",
        "hello": "Guten Tag,",
        "intro": (
            "Vielen Dank für Ihre Anfrage, darum kümmere ich mich gerne.",
            "Danke für Ihre Nachricht, hier ist unser Angebot für Sie.",
        ),
        "core": (
            (
                "Wir können Ihre Sendung von {origin} nach {destination} mit {carrier} für {total} {currency} all-in "
                "fliegen, die Laufzeit beträgt {transit}."
            ),
            (
                "Unser bestes Angebot von {origin} nach {destination} ist {carrier}: {total} {currency} all-in bei "
                "{transit} Laufzeit."
            ),
        ),
        "basis": "Grundlage sind {chargeable} kg frachtpflichtiges Gewicht; enthalten sind Luftfracht, Treibstoff- "
        "und Sicherheitszuschlag.",
        "basis_pickup": "Grundlage sind {chargeable} kg frachtpflichtiges Gewicht; enthalten sind die Abholung bei "
        "Ihnen durch {trucker}, Luftfracht, Treibstoff- und Sicherheitszuschlag.",
        "valid": "Der Preis gilt bis {until} UTC.",
        "book": (
            "Wenn das für Sie passt, antworten Sie einfach kurz, dann buche ich sofort.",
            "Antworten Sie einfach auf diese Mail, wenn Sie buchen möchten, den Rest übernehme ich.",
        ),
        "bye": "Viele Grüße\n{signer}\n" + COMPANY,
        "day": "Tag",
        "days": "Tage",
    },
}

PROSE_SYSTEM = (
    "You are an air export coordinator at a freight forwarder, replying by mail to a customer who asked for an air "
    "freight quote. Code writes the greeting, the price, the routing and the sign-off; you write only three short "
    "pieces, in the language with ISO code {language}, matching the customer's tone (German: always formal Sie). "
    "first_name: the customer's first name only if they signed the mail with it, else null. "
    "intro: one or two warm, natural sentences that show you read their mail: refer in words to the goods, the "
    "urgency, the timing or a question they asked. If they asked something a quote cannot answer, say you will "
    "come back to them on it. "
    "closing: one short friendly sentence for the very end, not a call to action and not a sign-off, or null. "
    "Never write digits, numbers, prices, currencies, dates, weekdays, months, airlines, airports, cities or "
    "countries. Do not greet and do not sign. Sound like a person, not a template: no 'we are pleased to', no "
    'exclamation marks. Answer as JSON {{"first_name": ..., "intro": ..., "closing": ...}}.'
)
NULLABLE = {"type": ["string", "null"]}
PROSE_FIELDS = {"first_name": NULLABLE, "intro": NULLABLE, "closing": NULLABLE}
PROSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": PROSE_FIELDS,
    "required": list(PROSE_FIELDS),
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
LIMITS = {"intro": 260, "closing": 140}
SUBJECT_MAX = 150
# Common words beyond extract's lists, so a short slot still shows its language; a slot must show ours.
MORE_EN = {"i", "it", "this", "that", "will", "be", "are", "in", "on", "have", "good", "great", "day", "thank",
           "happy", "hope", "glad", "here", "get", "keep", "so", "as", "all", "my", "me", "us", "an", "at"}  # fmt: skip
MORE_DE = {"ihnen", "ihre", "ihren", "ihrer", "gerne", "gern", "tag", "schönen", "einen", "noch", "vielen", "dank",
           "uns", "auf", "den", "dem", "bei", "es", "wird", "sind", "haben", "gute", "guten", "freue", "auch", "so",
           "hier", "ihrem", "ihres", "darum", "kümmere", "melde"}  # fmt: skip


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


def _body(rfq: Rfq, option: RateOption, prose: Prose, signer: str) -> str:
    lang = language_of(rfq)
    t = TEXT[lang]
    pick = rfq.session_id % 2
    core = t["core"][pick].format(
        origin=option.origin, destination=option.destination, carrier=option.carrier,
        total=money(option.total), currency=option.currency, transit=_transit(option, t),
    )  # fmt: skip
    chargeable = weight(option.chargeable_kg, lang)
    basis = (
        t["basis_pickup"].format(chargeable=chargeable, trucker=option.trucker)
        if option.trucker
        else t["basis"].format(chargeable=chargeable)
    )
    offer = [core, basis]
    if option.valid_until:
        offer.append(t["valid"].format(until=option.valid_until.astimezone(UTC).strftime("%d.%m.%Y %H:%M")))
    ending = [t["book"][pick]] + ([_sentence(prose.closing)] if prose.closing else [])
    lines = [
        t["hello_name"].format(name=prose.first_name) if prose.first_name else t["hello"],
        "",
        _sentence(prose.intro) if prose.intro else t["intro"][pick],
        "",
        " ".join(offer),
        "",
        " ".join(ending),
        "",
        t["bye"].format(signer=signer),
    ]
    return "\n".join(lines)


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
) -> tuple[str, str]:
    """Subject and body of the quote: one option, one all-in total with its currency and transit time, in the
    customer's language. Numbers are formatted by code from `option`, never by a model. `subject` is the
    customer's, answered with `Re:`. When the finished mail fails `check` or the slots break the rendering, it is
    rendered again without slots."""
    lang = language_of(rfq)
    title = reply_subject(subject) or TEXT[lang]["subject"].format(
        origin=option.origin, destination=option.destination, weight=weight(rfq.weight_kg, lang)
    )
    if prose and prose != Prose():
        try:
            body = _body(rfq, option, prose, signer)
            if check(body, rfq, option):
                return title, body
        except Exception:  # noqa: BLE001, S110  a slot must never cost the quote; render it plain
            pass
    return title, _body(rfq, option, Prose(), signer)


def _words_lang(text: str) -> tuple[int, int]:
    words = re.findall(r"[a-zäöüß]+", text.lower())
    return sum(w in GERMAN or w in MORE_DE for w in words), sum(w in ENGLISH or w in MORE_EN for w in words)


def _sentence_ok(text: str, slot: str, lang: str, banned: set[str]) -> bool:
    """Also drops a slot in a third language: it must show more of our language's words than of the other."""
    if not text or len(text) > LIMITS[slot] or "\n" in text or text.lstrip().startswith(("-", "*", "•")):
        return False
    if DIGIT.search(text) or CODE.search(text) or SYMBOL.search(text) or CALENDAR.search(text) or FRAME.search(text):
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


def clean(raw: dict, rfq: Rfq, message: Message, signer: str, banned: set[str] = frozenset()) -> Prose:
    """Each slot that fails a check becomes None: digits, three-letter codes, currency symbols, URLs, dates, a
    carrier's name, a greeting or sign-off, more than its length, another language, German `du`. The first name
    must stand in the customer's mail."""
    lang = language_of(rfq)
    raw = raw if isinstance(raw, dict) else {}
    banned = set(banned) | {w.casefold() for w in signer.split() if len(w) > 2}

    def text(key: str) -> str | None:
        value = raw.get(key)
        return " ".join(value.split()) if isinstance(value, str) and value.strip() else None

    intro, closing = text("intro"), text("closing")
    return Prose(
        first_name=_first_name(text("first_name"), message, signer),
        intro=intro if intro and _sentence_ok(intro, "intro", lang, banned) else None,
        closing=closing if closing and _sentence_ok(closing, "closing", lang, banned) else None,
    )


def adopt(ctx: Context, rfq: Rfq, message: Message, raw: dict) -> Prose:
    """Cleans the model's slots and stores them as `quote.prose`, so the fallback quote reads human too. Never
    raises: on any error the quote goes out with the fixed phrases."""
    try:
        prose = clean(raw, rfq, message, ctx.settings.signer, _banned(ctx))
        dropped = [k for k, v in asdict(prose).items() if isinstance(raw, dict) and raw.get(k) and not v]
        events.emit(ctx, "quote.prose", "rfq", session_id=rfq.session_id, payload={**asdict(prose), "dropped": dropped})
    except Exception as exc:  # noqa: BLE001
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id,
                    payload={"where": "prose.adopt", "error": type(exc).__name__})  # fmt: skip
        return Prose()
    return prose


async def prose(ctx: Context, rfq: Rfq, message: Message) -> Prose | None:
    """The quote's one small model call when rules read the request (R28, R31). None on any failure; never
    blocks the quote."""
    try:
        result = await llm.ask_json(
            ctx,
            purpose="quote.prose",
            system=PROSE_SYSTEM.format(language=language_of(rfq)),
            user=f"Subject: {message.subject}\n\n{message.body}"[:1500],
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
    try:
        row = ctx.db.execute("SELECT subject FROM messages WHERE id = ?", (message_id,)).fetchone()
    except Exception:  # noqa: BLE001  without it the quote keeps our own subject
        return None
    return row["subject"] if row else None
