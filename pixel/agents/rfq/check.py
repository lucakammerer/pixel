"""The last look at a quote before it leaves: invariants a correct quote always meets, checked by code alone.

Every rule here stands for a way a quote was lost (round 9: weight of the lots, 10: dangerous goods, 15: pickup
missing, R4/R16: a price the mail does not state). The check is independent of how the quote was built: it reads the
customer's mail, the RFQ, the chosen option, the options the session had and the finished text, and re-derives
what it can on its own instead of trusting the steps before it.

A finding is either BLOCK (the quote is very likely rejected as it stands) or WARN (worth a look). `agent._quote`
checks every quote it drafts, the fallback's too, and never holds or changes one: waiting for a missing pickup is
the orchestrator's job, and nothing else gets better by waiting. A BLOCK finding calls a person
(`feedback.alert`). The check never raises: an error inside it is one WARN finding.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from pixel.agents.rfq import compose
from pixel.agents.rfq.extract import DG_DOCUMENTS, number, own_words
from pixel.contracts import Preference, Quote, RateOption, Source
from pixel.pricing.calc import CBM_KG

BLOCK, WARN = "block", "warn"
CENTS = Decimal("0.03")  # parts are rounded one by one; the total once (docs/calc.md)
ACCEPT = Decimal("0.05")  # the scorer accepts within 5% of the best price
PER_KG = (Decimal("0.30"), Decimal(40))  # EUR per chargeable kg outside this is almost surely a misread


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str
    detail: str


def blocking(findings: list[Finding]) -> bool:
    return any(f.level == BLOCK for f in findings)


def as_payload(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]


def run(quote: Quote, request: str, options: list[RateOption] = (), attachments: tuple[str, ...] = ()) -> list[Finding]:
    """Every finding for `quote`. `request` is the customer's mail (subject and body), `options` every option the
    session found (airlines and pickups), `attachments` the file names on the customer's mail."""
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(quote, request, list(options), attachments))
        except Exception as exc:  # noqa: BLE001  a broken rule must never cost the quote
            findings.append(Finding(rule.__name__.lstrip("_"), WARN, f"check failed: {type(exc).__name__}: {exc}"))
    return findings


# --- the rules: each returns its findings -------------------------------------------------------------------------


def _required(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    r = q.rfq
    missing = [f for f in ("origin", "destination") if not getattr(r, f)]
    if r.weight_kg <= 0:
        missing.append("weight_kg")
    return [Finding("required", BLOCK, f"request without {', '.join(missing)}")] if missing else []


def _lane(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    o, r = q.option, q.rfq
    if (o.origin, o.destination) != (r.origin, r.destination):
        return [Finding("lane", BLOCK, f"option {o.origin}-{o.destination}, request {r.origin}-{r.destination}")]
    return []


def _arithmetic(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    o, found = q.option, []
    if q.total != o.total or q.currency != o.currency:
        found.append(Finding("total", BLOCK, f"quote {q.total} {q.currency}, option {o.total} {o.currency}"))
    if o.total <= 0 or o.total_eur <= 0:
        found.append(Finding("total", BLOCK, f"total {o.total} {o.currency} ({o.total_eur} EUR)"))
    parts = o.freight + o.fuel + o.security + o.pickup
    if abs(parts - o.total) > CENTS:
        found.append(Finding("total", BLOCK, f"parts add up to {parts}, total is {o.total}"))
    if o.currency == "EUR" and o.total_eur != o.total:
        found.append(Finding("total", BLOCK, f"EUR total {o.total} but total_eur {o.total_eur}"))
    if o.currency != "EUR" and not o.fx_eur:
        found.append(Finding("total", BLOCK, f"{o.currency} total without an exchange rate"))
    return found


def _chargeable(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """R11: chargeable = max(gross, cbm x 167); billed is never below it (a higher break may be billed)."""
    o, r = q.option, q.rfq
    needed = max(r.weight_kg, r.volume_cbm * CBM_KG)
    found = []
    if o.chargeable_kg + Decimal("0.01") < needed:
        found.append(Finding("chargeable", BLOCK, f"priced on {o.chargeable_kg} kg, the shipment needs {needed}"))
    if o.billed_kg + Decimal("0.01") < o.chargeable_kg:
        found.append(Finding("chargeable", BLOCK, f"billed {o.billed_kg} kg below chargeable {o.chargeable_kg}"))
    return found


def _pickup(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Door-to-port: the truck is part of the all-in price. Without it the quote is too low (round 15)."""
    o, r = q.option, q.rfq
    if r.door_pickup and (o.pickup <= 0 or not o.trucker):
        return [Finding("pickup", BLOCK, "the customer asked for pickup, the price holds no trucker")]
    if not r.door_pickup and (o.pickup > 0 or o.trucker):
        return [Finding("pickup", BLOCK, f"port-to-port request priced with pickup by {o.trucker}")]
    return []


def _best_price(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Our total against the cheapest total the session could have quoted for what the customer asked: the
    cheapest airline (cheapest) or the cheapest among the fastest (fastest), plus the cheapest pickup. More than
    5% above it is rejected when the scorer takes that one (round 16: the fastest trucker instead of the cheapest).
    WARN, not BLOCK: which trucker the scorer takes is learned from feedback (`pricing/selection.py`)."""
    o, r = q.option, q.rfq
    airlines = [a for a in options if a.trucker is None and a.total_eur > 0
                and (a.origin, a.destination) == (r.origin, r.destination)]  # fmt: skip
    pickups = [p.total_eur for p in options if p.trucker is not None and p.total_eur > 0]
    if not airlines or (r.door_pickup and not pickups):
        return []
    if r.preference == Preference.FASTEST:
        days = min(a.transit_days for a in airlines)
        airlines = [a for a in airlines if a.transit_days == days]
    best = min(a.total_eur for a in airlines) + (min(pickups) if r.door_pickup else 0)
    if o.total_eur > best * (1 + ACCEPT):
        return [Finding("best_price", WARN, f"{o.total_eur} EUR is {_pct(o.total_eur, best)} above {best} EUR, "
                                            "the cheapest the session had for this request")]  # fmt: skip
    if o.total_eur * (1 + ACCEPT) < best:
        return [Finding("best_price", WARN, f"{o.total_eur} EUR is below every option the session had ({best} EUR)")]
    return []


def _per_kg(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    o = q.option
    kg = max(o.billed_kg, o.chargeable_kg)
    if kg <= 0:
        return [Finding("per_kg", BLOCK, "no chargeable weight")]
    per_kg = o.total_eur / kg
    if not PER_KG[0] <= per_kg <= PER_KG[1]:
        return [Finding("per_kg", WARN, f"{per_kg:.2f} EUR per kg is outside {PER_KG[0]}-{PER_KG[1]}")]
    return []


# "Lot 2: 4 cartons, each 48 kg gross (41 kg net), ..." and bullet lists of the same shape.
LOT_START = re.compile(r"^\s*(?:[-*•]\s*)?(?:lot|pos(?:ition)?|item|part|teil|los)\s*\.?\s*\d+\s*[:.)-]", re.IGNORECASE)
LOT_COUNT = re.compile(
    r"\s*(\d+)\s*(?:x\s+)?[^\d\n]{1,40}?(?=\d|each|je|à|per\b|together|total|zusammen|insgesamt)", re.IGNORECASE
)
KG = re.compile(
    r"(each|je|per\s+(?:piece|unit|pc|stück)|à|(\d+)\s*[x×])?\s*(\d[\d.,]{0,12})\s*(?:kg|kgs|kilo\w*)\b", re.IGNORECASE
)
LINE_MAX = 400  # a lot line longer than this is not a lot line; it also keeps the regexes linear
NET_AFTER = re.compile(r"\s*\(?\s*(?:net|netto|nett)\b", re.IGNORECASE)
EACH_AFTER = re.compile(
    r"\s*(?:gross\s*|brutto\s*)?(?:each|je|per\s+(?:piece|unit|pc|stück)|pro\s+stück)\b", re.IGNORECASE
)


def lot_weight(text: str) -> Decimal | None:
    """Gross weight of a request given lot by lot, read independently of `extract._lots`. None unless there are
    at least two lot lines and every one of them reads as "<count> <packages> ... <gross> kg"."""
    total, lots = Decimal(0), 0
    for line in text.splitlines():
        start = LOT_START.match(line)
        if start and len(line) > LINE_MAX:
            return None
        if not start:
            continue
        head = LOT_COUNT.match(line, start.end())
        if not head:
            return None  # a lot we cannot read: no sum rather than a wrong one
        gross = [m for m in KG.finditer(line, start.end()) if not NET_AFTER.match(line, m.end())]
        kg = number(gross[0].group(3)) if gross else None
        if kg is None:
            return None
        if gross[0].group(2):  # "2 x 300 kg": the factor is written next to the figure
            total += kg * int(gross[0].group(2))
        else:
            each = bool(gross[0].group(1)) or bool(EACH_AFTER.match(line, gross[0].end()))
            total += kg * (int(head.group(1)) if each else 1)
        lots += 1
    return total if lots >= 2 else None


def _weight(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Round 9: the lots were added up wrong. Re-add them here and compare."""
    lots = lot_weight(own_words(request))
    if lots is None:
        return []
    weight = q.rfq.weight_kg
    if abs(lots - weight) > max(Decimal(1), lots * Decimal("0.01")):
        return [Finding("weight", BLOCK, f"the lots add up to {lots} kg, priced on {weight} kg")]
    return []


NEGATED = r"(?<!non-)(?<!non )(?<!no )(?<!not )(?<!kein )(?<!keine )(?<!nicht )"
DG_STRONG = re.compile(
    rf"\bUN\s?\d{{4}}\b|\bclass\s+[1-9](?:\.\d)?\b|\b{NEGATED}(?:dangerous goods|hazardous|hazmat|gefahrgut|"
    r"lithium|flammable|entzündlich|corrosive|ätzend|explosive|radioactive)\b(?!\s*[:=]?\s*(?:no|none|n/?a|nein|kein\w*)\b)",
    re.IGNORECASE,
)
DG_WEAK = re.compile(
    r"\b(?:adhesives?|glue|klebstoff\w*|kleber|paints?|lacke?|varnish|perfumes?|aerosols?|spray|batter(?:y|ies)|"
    r"batterien?|akkus?|resins?|harz\w*|solvents?|lösungsmittel|ink|tinte|fuel|kraftstoff|gas|magnets?)\b",
    re.IGNORECASE,
)
DG_NOTE = re.compile(r"\bDGD\b|shipper'?s declaration|versendererklärung|\bMSDS\b|sicherheitsdatenblatt", re.IGNORECASE)


def _dangerous(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Round 10: dangerous goods need the papers named in the quote (`dangerous.py`). Blocks when `extract`
    marked the request as dangerous goods and the note is missing; a word in the mail alone only warns, since
    "not classified as dangerous goods" or "class 2 cartons" read alike to a regex."""
    if DG_NOTE.search(q.body):
        return []
    if DG_DOCUMENTS in q.rfq.missing:
        return [Finding("dangerous_goods", BLOCK, "the request declares dangerous goods, the quote asks for no papers")]
    text = own_words(request)
    strong = DG_STRONG.search(text)
    if strong:
        return [Finding("dangerous_goods", WARN, f"'{strong.group(0)}' in the request, the quote asks for no papers")]
    weak = DG_WEAK.search(text)
    if weak:
        unread = f"; attachments {', '.join(files)} may say more" if files else ""
        return [Finding("dangerous_goods", WARN, f"'{weak.group(0)}' is often dangerous goods{unread}")]
    return []


MONEY = re.compile(
    r"(?:(?:EUR|USD|GBP|CHF|€|\$)\s?(\d[\d.,]{0,14}\d|\d))|(?:(\d[\d.,]{0,14}\d|\d)\s?(?:EUR|USD|GBP|CHF|€|\$))"
)
LEFTOVER = re.compile(r"\{[a-z_]+\}|\bNone\b|\bnan\b|\bnull\b|\bundefined\b|<[a-z_]+>|\[\[|\]\]")


def ours(body: str) -> str:
    """The mail without the dangerous-goods note: that quotes the customer's packing list, amounts and all."""
    return "\n\n".join(p for p in body.split("\n\n") if not DG_NOTE.search(p))


def amounts(body: str) -> list[set[Decimal]]:
    """Every amount with a currency next to it, each as the set of values it can be read as (1,234.56 or
    1.234,56): the scorer may read either."""
    found = []
    for m in MONEY.finditer(body):
        readings = {v for v in _readings(m.group(1) or m.group(2)) if v is not None}
        if readings:
            found.append(readings)
    return found


def _readings(raw: str) -> set[Decimal | None]:
    def parse(s: str) -> Decimal | None:
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    return {parse(raw.replace(",", "")), parse(raw.replace(".", "").replace(",", "."))}


def _text(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """R9/P1: the scorer reads one total out of the mail. It must be there exactly once, and no other amount
    with a currency may be (the R4 booking mail showed the airline's price, not the quote's)."""
    found = []
    total = compose.money(q.total)
    body = ours(q.body)
    count = body.count(total)
    if count != 1:
        found.append(Finding("text_total", BLOCK, f"the total {total} appears {count} times in the mail"))
    if q.currency not in q.body:
        found.append(Finding("text_total", BLOCK, f"the currency {q.currency} is not in the mail"))
    others = [min(r) for r in amounts(body) if q.total not in r and r != {Decimal(0)}]
    if others:
        found.append(
            Finding("text_amounts", BLOCK, f"other amounts with a currency in the mail: {others}, the quote is {total}")
        )
    if not q.subject.strip():
        found.append(Finding("text_empty", BLOCK, "empty subject"))
    if not body.strip():
        found.append(Finding("text_empty", BLOCK, "empty body"))
    elif leftover := LEFTOVER.search(body):  # the subject is the customer's, answered with Re:
        found.append(Finding("text_leftover", BLOCK, f"'{leftover.group(0)}' in the mail"))
    if q.option.carrier not in q.body:
        found.append(Finding("text_carrier", WARN, f"the carrier {q.option.carrier} is not named"))
    return found


def _language(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    de, en = compose._words_lang(q.body)
    lang = compose.language_of(q.rfq)
    if (lang == "de" and en > de * 2 and en >= 8) or (lang == "en" and de > en * 2 and de >= 8):
        return [Finding("language", WARN, f"customer writes {lang}, the mail reads {'en' if en > de else 'de'}")]
    return []


def _validity(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    until = q.option.valid_until
    if until is not None and until <= q.created_at:
        return [Finding("validity", WARN, f"the rate expired at {until.isoformat()}")]
    if q.option.source == Source.PORTAL and not q.option.rate_id:
        return [Finding("rate_id", WARN, "a portal option without a rate id cannot be booked on the portal")]
    return []


def _pct(value: Decimal, best: Decimal) -> str:
    return f"{(value / best - 1) * 100:.1f}%" if best else "?"


RULES = (_required, _lane, _arithmetic, _chargeable, _pickup, _best_price, _per_kg, _weight, _dangerous, _text,
         _language, _validity)  # fmt: skip
