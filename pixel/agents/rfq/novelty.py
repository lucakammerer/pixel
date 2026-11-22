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
from pixel.contracts import Rfq
from pixel.core import events, feedback, pdf, poller, timing
from pixel.core.context import Context
from pixel.pricing import store

WAIT_S = 8  # gpt-4.1 answers in about 1.5 s; it runs beside the bids, which take about 30 s
MAX_CHARS = 600
CONSUMED = (
    extract.PAIR, extract.PAIR_NAMED, extract.ORIGIN_LABEL, extract.DEST_LABEL, *extract.WEIGHT, *extract.VOLUME,
    *extract.POUNDS, *extract.TONNES, extract.DIMS, extract.PIECES, extract.FASTEST, extract.CHEAPEST,
    extract.HURRY, extract.NOT_URGENT, extract.DOOR, extract.OWN_DELIVERY, extract.POSTCODE, extract.DANGEROUS,
    extract.CURRENCY_ASK, extract.SIGN_OFF, compose.READY, compose.PICKUP_ASK, compose.DEPARTURE, compose.ONE_AWB,
    compose.CUTOFF, compose.GOODS, compose.FRAME, compose.WELL, compose.FROM_TOWN, pdf.REFERENCE,
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
    "service export import site airport departure truck trucking pickup pick-up collection transport "
    "bitte angebot luftfracht sendung anhang danke vielen gruss gruesse grüße unser unsere ihr ihre für mit und "
    "der die das den dem ein eine einen wir sie uns pour merci nous vous votre notre les des une avec avance "
    "d’avance d'avance bonne journée"
)
FRAME_WORDS = set(FRAME.split())
SYSTEM = (
    "An air freight customer's request. Code read the lane, cargo, pickup and the cheapest or fastest wish; these "
    "sentences are what it could not place. Map each requirement they set for this quote: airlines ruled out or "
    "the only airlines allowed (as written), the latest arrival date (YYYY-MM-DD, only as written in the "
    "sentences), documents the quote must name, and any other condition in a short phrase in the customer's "
    "language. Nothing about prices. Empty lists and null when the sentences set no requirement."
)
STRINGS = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "exclude_airlines": STRINGS,
        "only_airlines": STRINGS,
        "arrive_by": {"type": ["string", "null"]},
        "documents": STRINGS,
        "conditions": STRINGS,
    },
    "required": ["exclude_airlines", "only_airlines", "arrive_by", "documents", "conditions"],
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
MONEY = re.compile(rf"[€$£]|\b(?:{'|'.join(extract.CURRENCIES)})\b")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
WORD = re.compile(r"[^\W\d_][\w'’-]+")


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


def _named(found: list[str], said: str, airlines: set[str]) -> tuple[str, ...]:
    """Only names the sentences themselves hold, a model's guess never moves the selection; with the airline of our
    list each one stands for ("Zephyr Steppe" for "Zephyr Steppe Cargo")."""
    names = {a.strip() for a in found if a.strip() and a.strip().casefold() in said.casefold()}
    return tuple(sorted(names | {k for n in names for k in extract.excluded_airlines(f"not {n}", airlines)[0]}))


def _arrive_by(value: str | None, said: str) -> str | None:
    """The model's date only when the sentences write it out."""
    written = set()
    for m in compose.DATE.finditer(said):
        y, mo, d, dd, mm, yy = m.groups()
        try:
            written.add(date(int(y), int(mo), int(d)) if y else date(int(yy), int(mm), int(dd)))
        except ValueError:
            continue
    try:
        day = date.fromisoformat(value or "")
    except ValueError:
        return None
    return day.isoformat() if day in written else None


def _phrases(found: list[str], most: int) -> list[str]:
    """Short phrases for the quote's text, never one with a currency: the scorer reads our price from the mail."""
    return [p.strip() for p in found if p.strip() and len(p) <= 80 and not MONEY.search(p)][:most]


async def check(ctx: Context, rfq: Rfq, text: str, subject: str = "") -> Rfq:
    """Emits `rfq.novelty` with what is left over and what it maps to; the RFQ with airlines the sentences rule
    out. No call when nothing is left; at most WAIT_S, never raises."""
    try:
        airlines = store.airline_names(ctx)
        left = leftover(text, airlines, subject)
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
            "arrive_by": _arrive_by(data["arrive_by"], said),
            "documents": _phrases(data["documents"], 4),
            "conditions": _phrases(data["conditions"], 3),
        }
    except Exception as exc:  # noqa: BLE001  unread: the quote goes out as the rules read it, a person hears of it
        mapped = {"error": type(exc).__name__}
    events.emit(ctx, "rfq.novelty", "rfq", session_id=rfq.session_id, payload={"leftover": left, "mapped": mapped})
    if any(mapped.values()) and ctx.settings.alert_url:
        poller.spawn(ctx, feedback.post(ctx, [{"issue": "novel_requirement", "session": rfq.session_id,
                                               "at": timing.now().isoformat()[:19], "leftover": said,
                                               "mapped": mapped}]))  # fmt: skip
    if not mapped.get("exclude_airlines"):
        return rfq
    rfq = rfq.model_copy(update={"excluded_carriers": tuple(sorted({*rfq.excluded_carriers,
                                                                    *mapped["exclude_airlines"]}))})  # fmt: skip
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
    if not lines:
        return body
    paragraphs = body.split("\n\n")
    signed = next((i for i, p in enumerate(paragraphs) if COMPANY in p), len(paragraphs) - 1)
    at = max(signed - 1, 1)
    return "\n\n".join([*paragraphs[:at], " ".join(lines), *paragraphs[at:]])
