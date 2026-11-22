"""A requirement no rule reads, caught inside the round. The rounds only one to three teams win were each the first
of a new kind of requirement (rounds 10, 15, 33, 42, 76), and our repair after the verdict always came one round
late. `leftover` keeps the request's sentences no extractor consumes; only when some are left, one gateway call maps
them to what code acts on (an airline ruled out or required, an arrival date) or names in the quote (documents, a
condition), and `PIXEL_ALERT_URL` hears of it at once. The model never prices or selects."""

import asyncio
import json
import re
from datetime import date

from pixel import llm
from pixel.agents.rfq import compose, extract
from pixel.agents.rfq.compose import COMPANY
from pixel.contracts import Preference, Rfq
from pixel.core import events, feedback, pdf, poller, timing
from pixel.core.context import Context
from pixel.pricing import store

WAIT_S = 8  # gpt-4.1 answers in about 1.5 s; it runs beside the bids, which take about 30 s
MAX_CHARS = 600
CONSUMED = (
    extract.PAIR, extract.PAIR_NAMED, extract.ORIGIN_LABEL, extract.DEST_LABEL, *extract.WEIGHT, *extract.VOLUME,
    *extract.POUNDS, *extract.TONNES, extract.DIMS, extract.PIECES, extract.FASTEST, extract.CHEAPEST,
    extract.HURRY, extract.NOT_URGENT, extract.DOOR, extract.OWN_DELIVERY, extract.POSTCODE, extract.DANGEROUS,
    extract.CURRENCY_ASK, extract.SIGN_OFF, compose.READY, extract.READY_ON, extract.ARRIVE_BY, extract.ARRIVE_BY_LAST, extract.MAX_TRANSIT,
    compose.PICKUP_ASK, compose.DEPARTURE, compose.ONE_AWB,
    compose.CUTOFF, compose.BREAKDOWN, compose.ADDRESSEE, *compose.SERVICES.values(), extract.ASKED_LANGUAGE,
    compose.GOODS, compose.FRAME, compose.WELL, compose.FROM_TOWN, pdf.REFERENCE,
    re.compile(r"\bPDF\b"),  # every quote carries itself as a PDF (`core/pdf.py`, round 76)
    re.compile(r"\([A-Z]{3}\)|\b[A-Z]{3} \("),  # an airport named with its city: the lane
    re.compile(r"\bearliest\b.{0,30}\barrival|frühest\w* Ankunft", re.IGNORECASE),  # `compose.answers`
    re.compile(r"non-?dangerous|not restricted|general cargo|kein Gefahrgut"
               r"|(?:dangerous goods|gefahrgut):\s*(?:no|none|n/?a|nein)\b", re.IGNORECASE),
    # pointing to a file: what it holds is read with it
    re.compile(r"attached|enclosed|\banbei\b|\banhang|angehängt\w*|pièce jointe|ci-joint|\bvoice\b|record\w*"
               r"|\.[a-z]{3,4}\)", re.IGNORECASE),
    # a document's fields and its table header: parties, goods, the measurement columns
    re.compile(r"^\W*(?:shipper|consignee|notify|goods description|description|commodity|"
               r"versender|empfänger|warenbeschreibung)\s*:|\((?:cm|mm|m|kgs?|lbs?)\)"
               r"|^[^:]{1,30}:\s*[\d.,]+\s*\w{0,5}$", re.IGNORECASE),
)  # fmt: skip
TITLE = r"(?:[A-ZÄÖÜ][\w'’.&,-]*|of|and|for|und|für|de|/|&|-)"
HEADING = re.compile(rf"^{TITLE}(?:\s+{TITLE}){{0,5}}$")  # a name, a company, a document's title
# What every request says around its facts: asking for a price, pointing to an attachment, thanking.
FRAME = (
    "the and for with our your you please could would can kindly send quote quotation offer price rate rates all-in "
    "allin air freight airfreight option options shipment cargo goods request inquiry enquiry details attached "
    "find below above this that these those from via this also include including "
    "included arrange thank thanks advance look looking forward hear soon possible best regards hello dear team "
    "sir madam taking lot have lovely day nice week good hope going well reply mail email back let know will "
    "need needs are was were has had all any some one per total gross weight volume pieces pcs ready date "
    "service export import site airport departure truck trucking pickup pick-up collection transport arranging flight their i’m i'm sorry left out "
    "bitte angebot luftfracht sendung anhang danke vielen gruss gruesse grüße unser unsere ihr ihre für mit und "
    "der die das den dem ein eine einen wir sie uns pour merci nous vous votre notre les des une avec avance "
    "d’avance d'avance bonne journée"
)
FRAME_WORDS = set(FRAME.split())
SYSTEM = (
    "An air freight customer's request. Code read the lane, cargo and pickup; these sentences are what it could "
    "not place. Say whether they ask for the cheapest or the fastest option (wish: cheapest, fastest, or none when "
    "they do not say). Map each requirement they set for this quote: airlines ruled out or "
    "the only airlines allowed (as written), the latest arrival date and the date the goods are ready or can be "
    "collected (YYYY-MM-DD, only as written in the sentences), documents the quote must name, and any other "
    "condition in a short phrase in the customer's language. Nothing about prices. Empty lists and null when the "
    "sentences set no requirement."
)
STRINGS = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "exclude_airlines": STRINGS,
        "only_airlines": STRINGS,
        "arrive_by": {"type": ["string", "null"]},
        "ready_on": {"type": ["string", "null"]},
        "documents": STRINGS,
        "conditions": STRINGS,
        "wish": {"type": "string", "enum": ["cheapest", "fastest", "none"]},
    },
    "required": ["exclude_airlines", "only_airlines", "arrive_by", "ready_on", "documents", "conditions", "wish"],
    "additionalProperties": False,
}
NOTE = {
    "en": ("Documents we will need: {}.", "Noted from your request: {}."),
    "de": ("Benötigte Dokumente: {}.", "Aus Ihrer Anfrage vermerkt: {}."),
    "fr": ("Documents nécessaires : {}.", "Noté de votre demande : {}."),
}
AIRLINE_ALTERNATIVE = {
    "en": "We could not get a rate from the airline you specified. The price above is for the alternative shown.",
    "de": "Für die gewünschte Airline haben wir keinen Tarif erhalten. Der Preis oben gilt für die genannte Alternative.",
    "fr": "Nous n’avons pas obtenu de tarif auprès de la compagnie demandée. Le prix ci-dessus concerne l’alternative indiquée.",
}
SCHEDULE_ALTERNATIVE = {
    "en": "The available option falls outside the timing you requested.",
    "de": "Die verfügbare Option liegt außerhalb des gewünschten Zeitrahmens.",
    "fr": "L’option disponible dépasse le délai que vous avez demandé.",
}
MONEY = re.compile(rf"[€$£]|\b(?:{'|'.join(extract.CURRENCIES)})\b")
# No split after a dotted abbreviation: "Corrosive liquid, n.o.s. (contains phosphoric acid)" stays one sentence.
# Clauses too: "X only, and the cargo must arrive by ..." holds two requirements; a rule reading one must not hide
# the other.
SENTENCE = re.compile(r"(?<=[.!?])(?<!\.\w\.)\s+|\n|;\s*|,\s+(?=(?:and|but|while|und|aber|et|mais)\s)")
WORD = re.compile(r"[^\W\d_][\w'’-]+")
DATE = re.compile(
    rf"(?<![\w.,/-])(?P<value>{extract.DATE_VALUE}|\d{{1,2}}/\d{{1,2}}(?:/\d{{4}})?)(?![\w/-])", re.IGNORECASE
)
# A date right after these words is not the shipment's: our answer by a date, a quote's validity.
SET_ASIDE = re.compile(
    r"\b(?:reply|respond|answer|quote|quotation|offer|send|confirm|advise|let us know|feedback|valid\w*"
    r"|antwort\w*|rückmeld\w*|angebot\w*|senden|schicken|bestätig\w*|mitteil\w*|gültig\w*"
    r"|répon\w*|envoy\w*|devis|offre|valable)\b(?:\W+\w+){0,3}\W*$",
    re.IGNORECASE,
)


def leftover(text: str, airlines: set[str] = frozenset(), subject: str = "") -> list[str]:
    """The sentences of the mail and its documents no rule reads: none of the extractors' patterns, no airline
    ruled out by name, and more than the frame every request has. The subject, a signature below a sign-off and
    lines of names or headings ("Mira Petersen", "Packing List") are skipped."""
    left, signed = [], False
    for line in text.splitlines():
        if line.strip() == subject.strip() or HEADING.match(line.strip()):
            continue
        if not line.strip():
            signed = False
            continue
        if signed or extract.SIGN_OFF.match(line):
            signed = True
            continue
        # Keep the period in an order-number abbreviation from splitting the reference in two.
        line = pdf.REFERENCE.sub(lambda match: match[0].replace(".", ""), line)
        for sentence in SENTENCE.split(line):
            words = [w for w in WORD.findall(sentence.casefold()) if w not in FRAME_WORDS and len(w) > 2]
            if not words or any(p.search(sentence) for p in CONSUMED):
                continue
            if airlines and (
                extract.required_airlines(sentence, airlines) or extract.excluded_airlines(sentence, airlines)[0]
            ):
                continue
            left.append(sentence.strip())
    return left


def _date(value: str, on: date) -> date | None:
    """A written date resolved like the rules' (`extract._stated_date`); "26/11" day first, else month first."""
    if "/" not in value:
        return extract._stated_date(value, on)
    first, second, *year = value.split("/")
    return extract._stated_date(f"{first}.{second}.{''.join(year)}", on) or extract._stated_date(
        f"{second}.{first}.{''.join(year)}", on
    )


def unread_dates(text: str, rfq: Rfq) -> list[str]:
    """The sentences with a coming date that no field holds (round 118: "Die Ware steht am 2026-11-26 zur Abholung
    bereit" counted as read because another rule used the sentence, so the transit was measured from today). A date
    is accounted for as the ready date or the latest arrival the rules read, as a reply-by or a quote's validity
    (`SET_ASIDE`), below the sign-off, or when it is not after the request day: that moves no price."""
    on, read, left, signed = rfq.received_at.date(), {rfq.ready_on, rfq.arrive_by}, [], False
    for line in text.splitlines():
        signed = bool(line.strip()) and (signed or bool(extract.SIGN_OFF.match(line)))
        for sentence in [] if signed else SENTENCE.split(line):
            if any(
                (day := _date(m["value"], on)) and day > on and day not in read
                and not SET_ASIDE.search(sentence[: m.start()])
                for m in DATE.finditer(sentence)
            ):  # fmt: skip
                left.append(sentence.strip())
    return left


def _named(found: list[str], said: str, airlines: set[str]) -> tuple[str, ...]:
    """Only names the sentences themselves hold, a model's guess never moves the selection; with the airline of our
    list each one stands for ("Zephyr Steppe" for "Zephyr Steppe Cargo")."""
    names = {a.strip() for a in found if a.strip() and a.strip().casefold() in said.casefold()}
    return tuple(sorted(names | {k for n in names for k in extract.excluded_airlines(f"not {n}", airlines)[0]}))


def _as_written(value: str | None, said: str, on: date) -> str | None:
    """The model's date only when the sentences write it out."""
    try:
        day = date.fromisoformat(value or "")
    except ValueError:
        return None
    return day.isoformat() if day in {_date(m["value"], on) for m in DATE.finditer(said)} else None


def _phrases(found: list[str], most: int) -> list[str]:
    """Short phrases for the quote's text, never one with a currency: the scorer reads our price from the mail."""
    return [p.strip() for p in found if p.strip() and len(p) <= 80 and not MONEY.search(p)][:most]


async def check(ctx: Context, rfq: Rfq, text: str, subject: str = "", mail: str | None = None) -> Rfq:
    """Emits `rfq.novelty` with what is left over and what it maps to; the RFQ with airlines the sentences rule
    out and the ready date the rules left empty. `mail` is the customer's own words, whose dates must each be
    accounted for (`unread_dates`); a document's dates are not. No call when nothing is left; at most WAIT_S,
    never raises."""
    try:
        airlines = store.airline_names(ctx)
        left = leftover(text, airlines, subject)
        left += [s for s in unread_dates(text if mail is None else mail, rfq) if s not in left]
    except Exception as exc:  # noqa: BLE001  the check must never cost the quote
        events.emit(ctx, "error", "rfq", session_id=rfq.session_id, payload={"where": "novelty", "error": repr(exc)})
        return rfq
    if not left:
        events.emit(ctx, "rfq.novelty", "rfq", session_id=rfq.session_id, payload={"leftover": []})
        return rfq
    said = "\n".join(left)[:MAX_CHARS]
    try:
        result = await asyncio.wait_for(
            llm.ask_json(ctx, purpose="rfq.novelty", system=SYSTEM, user=said, schema=SCHEMA,
                         session_id=rfq.session_id, max_output_tokens=150, model=ctx.settings.model_check or None),
            WAIT_S,
        )  # fmt: skip
        data = result.data
        mapped = {
            "exclude_airlines": _named(data["exclude_airlines"], said, airlines),
            "only_airlines": _named(data["only_airlines"], said, airlines),
            "arrive_by": _as_written(data["arrive_by"], said, rfq.received_at.date()),
            "ready_on": _as_written(data["ready_on"], said, rfq.received_at.date()),
            "documents": _phrases(data["documents"], 4),
            "conditions": _phrases(data["conditions"], 3),
        }
        wish = data.get("wish") if data.get("wish") in ("cheapest", "fastest") else None
    except Exception as exc:  # noqa: BLE001  unread: the quote goes out as the rules read it, a person hears of it
        mapped, wish = {"error": type(exc).__name__}, None
    # The model reads the cheapest or fastest wish in any language; it decides only where no rule read one (round
    # 186: "en hızlı seçenek" became a note, and the cheapest airline was quoted). Rules that read one win.
    stated = extract.stated_preference(f"{subject}\n{text}") if rfq.extracted_by == "rules" else rfq.preference
    preference = Preference(wish) if wish and stated is None else None
    payload = {"leftover": left, "mapped": mapped}
    if wish:
        payload["wish"] = wish
        if stated is not None and stated.value != wish:
            payload["wish_conflict"] = stated.value
    events.emit(ctx, "rfq.novelty", "rfq", session_id=rfq.session_id, payload=payload)
    if any(mapped.values()) and ctx.settings.alert_url:
        poller.spawn(ctx, feedback.post(ctx, [{"issue": "novel_requirement", "session": rfq.session_id,
                                               "at": timing.now().isoformat()[:19], "leftover": said,
                                               "mapped": mapped}]))  # fmt: skip
    update = {"preference": preference} if preference and preference != rfq.preference else {}
    if mapped.get("exclude_airlines"):
        update["excluded_carriers"] = tuple(sorted({*rfq.excluded_carriers, *mapped["exclude_airlines"]}))
    ready = date.fromisoformat(mapped["ready_on"]) if mapped.get("ready_on") else None
    if ready and rfq.ready_on is None and (rfq.arrive_by is None or ready < rfq.arrive_by):  # the rules' date wins
        update["ready_on"] = ready
    if not update:
        return rfq
    rfq = rfq.model_copy(update=update)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, message_id=rfq.message_id,
                payload=rfq.model_dump(mode="json"))  # fmt: skip
    return rfq


def mapped(ctx: Context, session_id: int) -> dict:
    """The session's last mapped requirements, {} when there are none or they cannot be read."""
    try:
        row = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.novelty' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        found = json.loads(row["payload"]).get("mapped") if row else None
        return found if isinstance(found, dict) and "error" not in found and any(found.values()) else {}
    except Exception:  # noqa: BLE001
        return {}


def with_note(body: str, requirements: dict, language: str) -> str:
    """The documents and conditions named before the paragraph above the signature, as `dangerous.with_note`."""
    docs, noted = NOTE.get(language, NOTE["en"])
    lines = [t.format(", ".join(items)) for t, items in ((docs, requirements.get("documents")),
                                                         (noted, requirements.get("conditions"))) if items]  # fmt: skip
    if requirements.get("unmet_carrier"):
        lines.append(AIRLINE_ALTERNATIVE.get(language, AIRLINE_ALTERNATIVE["en"]))
    if requirements.get("unmet_schedule"):
        lines.append(SCHEDULE_ALTERNATIVE.get(language, SCHEDULE_ALTERNATIVE["en"]))
    if not lines:
        return body
    paragraphs = body.split("\n\n")
    signed = next((i for i, p in enumerate(paragraphs) if COMPANY in p), len(paragraphs) - 1)
    at = max(signed - 1, 1)
    return "\n\n".join([*paragraphs[:at], " ".join(lines), *paragraphs[at:]])
