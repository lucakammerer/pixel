"""The last look at a quote before it leaves: invariants a correct quote always meets, checked by code alone.

Every rule here stands for a way a quote was lost (round 9: weight of the lots, 10: dangerous goods, 15: pickup
missing, R4/R16: a price the mail does not state; a weight the mail's own figures do not give: 480 lbs as kg, a
per-piece weight as the total, a lot left out, the net weight). The check is independent of how the quote was built: it reads the
customer's mail, the RFQ, the chosen option, the options the session had and the finished text, and re-derives
what it can on its own instead of trusting the steps before it.

A finding is either BLOCK (the quote is very likely rejected as it stands) or WARN (worth a look). `agent._quote`
checks every quote it drafts, the fallback's too, and never holds or changes one: waiting for a missing pickup is
the orchestrator's job, and nothing else gets better by waiting. A BLOCK finding calls a person
(`feedback.alert`). The check never raises: an error inside it is one WARN finding.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
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


def run(
    quote: Quote,
    request: str,
    options: list[RateOption] = (),
    attachments: tuple[str, ...] = (),
    document_weight: Decimal | None = None,
) -> list[Finding]:
    """Every finding for `quote`. `request` is the customer's mail (subject and body), `options` every option the
    session found (airlines and pickups), `attachments` the file names on the customer's mail, `document_weight` the
    gross weight `extract` took from a same-lane document whose own table adds up to it (`rfq.evidence`)."""
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(quote, request, list(options), attachments))
        except Exception as exc:  # noqa: BLE001  a broken rule must never cost the quote
            findings.append(Finding(rule.__name__.lstrip("_"), WARN, f"check failed: {type(exc).__name__}: {exc}"))
    if document_weight is not None and _close(quote.rfq.weight_kg, document_weight):
        # Round 35: the spec's 1751 kg replaces the mail's 2644 kg; the mail's figures cannot explain it.
        findings = [f for f in findings if f.rule not in ("weight", "weight_unexplained")]
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


# --- weight accounting: the priced weight must be one the mail's own figures explain --------------------------------
#
# Independent of how `extract` read the mail: every mass figure the customer wrote (kg, lb, t, or a labelled
# "weight: 480") is collected with what stands next to it (net, a limit, per piece, the piece count before it, a
# total). The priced weight passes only when one plain reading of those figures gives it: one figure, one figure
# times its pieces, the sum of the groups, or the lots added up. Every live misread so far fails this test: "480 lbs"
# priced as 480 kg, "4 cartons, each 48 kg" as 48 kg, a lot left out of the sum. It makes no model call and changes
# no price.

MASS_UNIT = r"(?P<unit>kgs?|kilo(?:gram(?:m|s)?|s)?|lbs?|pounds?|t(?![-\w])|tons?|tonnes?|tonnen)"
MASS = re.compile(
    rf"(?P<num>\d{{1,3}}(?:[ \u00a0\u202f]\d{{3}})+(?![\d.,])|\d[\d.,']*\d|\d)\s*{MASS_UNIT}(?![\w])", re.IGNORECASE
)
MASS_LABEL = re.compile(  # "Gross weight: 480" without a unit is read as kg by the rules; read it the same way here
    r"\b(?:gross\s+weight|weight|gewicht|bruttogewicht)(?:\s*\(kgs?\))?\s*:\s*(?P<num>\d[\d.,']*\d|\d)"
    r"(?![\d.,']*\s*[a-z])",
    re.IGNORECASE,
)
MASS_NET = re.compile(r"\s*(?:kg|kgs)?\s*\)?\s*(?:net|netto|nett)\b|\s*\(\s*net", re.IGNORECASE)
MASS_NET_BEFORE = re.compile(r"(?:net|netto|nett)(?:\s+weight|gewicht)?\s*[:=]?\s*$", re.IGNORECASE)
MASS_EACH_BEFORE = re.compile(r"(?:each|apiece|je|à|per\s+\w+|pro\s+\w+)\s*(?:of\s+)?$", re.IGNORECASE)
MASS_EACH_AFTER = re.compile(
    r"\s*(?:gross\s*|brutto\s*)?(?:each|apiece|je|per\s+(?:piece|unit|pc|carton|case|drum|pallet|box|crate|stück)"
    r"|pro\s+(?:stück|palette|karton))\b",
    re.IGNORECASE,
)
FILLER = r"(?:\s+(?:approx\.?|approximately|ca\.?|circa|about|around|roughly|is|of|rund|etwa|von))*"
# Figures that are not the shipment's gross weight: a limit, a capacity, a chargeable or volume weight, what a truck
# carries or a pallet is rated for, an earlier shipment ("last time we shipped 800 kg").
MASS_OTHER = re.compile(
    r"(?:max(?:imum|\.)?|up\s+to|bis\s+zu|limit|capacity|chargeable|volumetric|volume\s+weight|frachtpflichtig\w*"
    rf"|volumengewicht|carr(?:y|ies)|rated\s+(?:for|at)|holds?)(?:\s+weight)?{FILLER}\s*:?\s*$"
    r"|\b(?:last\s+(?:time|month|week|year)|previous(?:ly)?|letztes\s+mal)\b[^.\n]*$",
    re.IGNORECASE,
)
MASS_TOTAL = re.compile(
    rf"\b(?:total|together|combined|altogether|in\s+total|gesamt|insgesamt|zusammen){FILLER}\s*:?\s*$", re.IGNORECASE
)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "a": 1, "an": 1, "ein": 1, "eine": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5}  # fmt: skip
PACKAGES = (
    r"(?:pcs|pieces?|pallets?|colli|packages?|cartons?|boxes|box|cases?|drums?|crates?|units?|stück|stk|paletten"
    r"|palette|kartons?|kisten?|fässer|fass)"
)
MASS_COUNT = re.compile(rf"\b(\d+|{'|'.join(WORDS)})\s*(?:x\s+)?{PACKAGES}\b", re.IGNORECASE)
GROUP_GAP = 30  # a count this close before a figure, with no other figure between, makes it a piece group's figure
# "300 kg of motors plus 250 kg of spare parts and 220 kg of cables": parts the prose itself adds up. Only words
# between the two figures, no number and no sentence break or bracket ("218 kg (480 lbs)", "700 kg. A 24 t truck").
JOINED = re.compile(r"^[^\d.;:!?()/]*?(?:,|\+|\b(?:plus|and|und|sowie|as\s+well\s+as|together\s+with|along\s+with"
                    r"|in\s+addition\s+to)\b)[^\d.;:!?()/]*$", re.IGNORECASE)  # fmt: skip
MASS_FACTOR = re.compile(r"(\d+)\s*[x×]\s*$", re.IGNORECASE)  # "2 x 300 kg"
TO_KG = {"lb": Decimal("0.45359237"), "t": Decimal(1000)}


@dataclass(frozen=True)
class Mass:
    """One mass figure of the mail: its readings in kg (1.234 may be 1234 or 1.234) and what stands next to it."""

    kg: frozenset[Decimal]
    per_piece: tuple[int, ...] | None  # the piece counts it may be for; None for a figure of a whole lot or shipment
    other: bool  # a limit, capacity or chargeable weight: never required to add up, may still match
    total: bool = False  # "total", "together": the sum of what came before it on its line or in the mail
    group: bool = False  # a piece count stands right before it: one group of the shipment ("2 pallets, 900 kg")
    line: int = 0

    def values(self) -> frozenset[Decimal]:
        if self.per_piece is None:
            return self.kg
        return frozenset(kg * n for kg in self.kg for n in self.per_piece)


def _count(raw: str) -> int:
    return int(raw) if raw.isdigit() else WORDS[raw.lower()]


def masses(text: str) -> list[Mass]:
    found = []
    for number_of_line, line in enumerate(text.splitlines()):
        if len(line) > LINE_MAX * 2:
            continue
        hits = [(m, m.group("unit")) for m in MASS.finditer(line)]
        hits += [(m, "kg") for m in MASS_LABEL.finditer(line) if not any(h.start() <= m.start("num") < h.end()
                                                                         for h, _ in hits)]  # fmt: skip
        hits.sort(key=lambda h: h[0].start("num"))
        previous_end, previous = 0, None
        for m, unit in hits:
            before, after = line[: m.start("num")], line[m.end() :]
            since = line[previous_end : m.start("num")]  # the words between the last figure and this one
            previous_end = m.end()
            if MASS_NET.match(after) or MASS_NET_BEFORE.search(before[-20:]):
                continue
            u = unit.lower()
            factor = TO_KG["lb"] if u.startswith(("lb", "pound")) else TO_KG["t"] if u.startswith("t") else Decimal(1)
            raw = m.group("num")
            parts = {raw, raw.split()[-1]} if raw.split()[1:] else {raw}  # "1 250 kg" is 1250, or "1" then 250
            readings = {v * factor for r in parts for t in (True, False) if (v := number(r, t)) is not None and v > 0}
            if not readings:
                continue
            # The counts since the figure before: "2 pallets: 400 kg; 3 boxes, each 100 kg" is 3 x 100, never 2 x 100.
            counts = [_count(c) for c in MASS_COUNT.findall(since) or MASS_COUNT.findall(before)]
            per_piece = None
            if (f := MASS_FACTOR.search(before)) is not None:
                per_piece = (int(f.group(1)),)
            elif MASS_EACH_BEFORE.search(before[-25:]) or MASS_EACH_AFTER.match(after):
                per_piece = tuple(sorted(set(counts)))  # nested groups ("2 pallets (8 cartons)"): every count
            near = since[-GROUP_GAP:]
            mass = Mass(frozenset(readings), per_piece, bool(MASS_OTHER.search(before[-40:])),
                        total=bool(MASS_TOTAL.search(before[-30:])),
                        group=per_piece is not None or bool(MASS_COUNT.search(near)), line=number_of_line)  # fmt: skip
            if _joined(previous, mass, since):
                found[-1] = replace(found[-1], group=True)
                mass = replace(mass, group=True)
            found.append(mass)
            previous = mass
    return _one_per_group(found)


def _joined(previous: Mass | None, mass: Mass, since: str) -> bool:
    """Two figures the prose adds up, each then one group of the shipment; not a figure written twice."""
    if previous is None or previous.other or mass.other or mass.total or not JOINED.match(since):
        return False
    return not any(_close(a, b) for a in previous.values() for b in mass.values())


def _one_per_group(figures: list[Mass]) -> list[Mass]:
    """A line that gives a group both per piece and as a total ("4 drums, 48 kg each, together 192 kg") names one
    weight twice: keep the total."""
    dropped, grouped = set(), set()
    for i, f in enumerate(figures):
        same = [j for j, t in enumerate(figures) if t.line == f.line and t.total and j != i and t.values() & f.values()]
        if f.per_piece is not None and same:
            dropped.add(i)
            grouped.update(same)  # the total stands for the group the per-piece figure was
    return [replace(f, group=True) if i in grouped else f for i, f in enumerate(figures) if i not in dropped]


def explanations(figures: list[Mass], listed: bool) -> set[Decimal]:
    """Every weight in kg a plain reading of the figures gives. A mail that gives its shipment group by group (a
    list, or two or more figures each right after a piece count) is explained only by the sum of the groups or by a
    total that equals it: one group alone is a lot left out (922 kg priced for 1822 kg). Any other mail by any one of
    its figures, or the sum of all of them: a figure that is not the shipment ("a 24 t truck", "last month 1200 kg")
    must not hide the one that is."""
    shipment = [f for f in figures if not f.other and f.values()]
    if not shipment:
        return {v for f in figures for v in f.values()}
    low = [min(f.values()) for f in shipment]  # the usual reading: 1.234 kg is 1234 kg, 1,5 t is 1500 kg
    high = [max(f.values()) for f in shipment]
    for i, f in enumerate(shipment):  # a total: one figure that is the sum of all the others
        rest = sum(low[:i] + low[i + 1 :], Decimal(0))
        if len(shipment) > 2 and any(_close(v, rest) for v in f.values()):
            return set(f.values())
    out = {sum(low, Decimal(0)), sum(high, Decimal(0))}
    groups = [f for f in shipment if f.group]  # "4 drums, together 730 kg" is one group's total
    totals = [v for f in shipment if f.total and not f.group for v in f.values()]  # the whole shipment's
    if (listed or len(groups) >= 2) and len(shipment) > 1:
        group_sums = {
            sum((min(f.values()) for f in groups), Decimal(0)),
            sum((max(f.values()) for f in groups), Decimal(0)),
        }
        return out | (group_sums if groups else set()) | set(totals)
    return out | {v for f in shipment for v in f.values()}


LISTED = re.compile(r"^\s*(?:[-*•]|(?:lot|pos(?:ition)?|item|part|teil|los)\s*\.?\s*\d+\s*[:.)-])", re.IGNORECASE)


def listed(text: str) -> bool:
    """Two or more list lines that each carry a weight: the shipment is given line by line."""
    return sum(1 for line in text.splitlines() if LISTED.match(line) and masses(line)) >= 2


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.5"), b * Decimal("0.005"))


def _accounting(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """The priced weight is one the mail's own figures explain, else BLOCK. A mail that writes no weight at all
    only warns: the weight may come from an attachment the check cannot read."""
    text = own_words(request)
    weight = q.rfq.weight_kg
    figures = masses(text)
    if not figures:  # nothing to hold the weight against: an attachment, a mail we could not load
        return [Finding("weight_unexplained", WARN, f"priced on {weight} kg, the mail writes no weight")]
    ways = explanations(figures, listed(text))
    if (lots := lot_weight(text)) is not None:
        ways.add(lots)
    if any(_close(weight, w) for w in ways):
        return []
    shown = ", ".join(f"{w:g} kg" for w in sorted(ways)[:6]) if ways else "none"
    each = [f for f in figures if f.per_piece == ()]
    hint = "; a weight per piece without a piece count" if each else ""
    return [Finding("weight_unexplained", BLOCK, f"priced on {weight} kg; the mail's figures give {shown}{hint}")]


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


RULES = (_required, _lane, _arithmetic, _chargeable, _pickup, _best_price, _per_kg, _weight, _accounting, _dangerous, _text,
         _language, _validity)  # fmt: skip
