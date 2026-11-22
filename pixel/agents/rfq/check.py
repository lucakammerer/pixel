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

from pixel.agents.rfq import compose, extract
from pixel.agents.rfq.extract import DG_DOCUMENTS, number, own_words, shipments
from pixel.contracts import Preference, Quote, RateOption, Source
from pixel.core import pdf
from pixel.pricing.calc import CBM_KG, insurance_in_currency
from pixel.pricing.selection import TRUST, allowed, implausible

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
    *,
    document_weight: Decimal | None = None,
    insurance_eur: Decimal = Decimal(0),
) -> list[Finding]:
    """Every finding for `quote`. `request` is the customer's mail (subject and body), `options` every option the
    session found (airlines and pickups), `attachments` the file names on the customer's mail. A gross weight
    corroborated by a packing table can supersede the figures in the mail."""
    findings: list[Finding] = []
    for rule in RULES:
        if rule in (_weight, _accounting) and document_weight == quote.rfq.weight_kg:
            continue
        try:
            kwargs = {"insurance_eur": insurance_eur} if rule in (_arithmetic, _best_price, _insured) else {}
            findings.extend(rule(quote, request, list(options), attachments, **kwargs))
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


def _arithmetic(
    q: Quote, request: str, options: list[RateOption], files: tuple[str, ...], *, insurance_eur: Decimal
) -> list[Finding]:
    o, found = q.option, []
    if q.total != o.total or q.currency != o.currency:
        found.append(Finding("total", BLOCK, f"quote {q.total} {q.currency}, option {o.total} {o.currency}"))
    if o.total <= 0 or o.total_eur <= 0:
        found.append(Finding("total", BLOCK, f"total {o.total} {o.currency} ({o.total_eur} EUR)"))
    parts = o.freight + o.fuel + o.security + o.pickup + insurance_in_currency(o, insurance_eur)
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


def _best_price(
    q: Quote, request: str, options: list[RateOption], files: tuple[str, ...], *, insurance_eur: Decimal
) -> list[Finding]:
    """Our total against the cheapest total the session could have quoted for what the customer asked: the
    cheapest airline (cheapest) or the cheapest among the fastest (fastest), plus the cheapest pickup. More than
    5% above it is rejected when the scorer takes that one (round 16: the fastest trucker instead of the cheapest).
    WARN, not BLOCK: which trucker the scorer takes is learned from feedback (`pricing/selection.py`)."""
    o, r = q.option, q.rfq
    peers = allowed(r, options) or options
    airlines = [a for a in options if a.trucker is None and a.total_eur > 0 and not implausible(a, peers)
                and (a.origin, a.destination) == (r.origin, r.destination)]  # fmt: skip
    pickups = [p.total_eur for p in options if p.trucker is not None and p.total_eur > 0 and not implausible(p, peers)]
    if not airlines or (r.door_pickup and not pickups):
        return []
    if r.preference == Preference.FASTEST:
        days = min(a.transit_days for a in airlines)
        airlines = [a for a in airlines if a.transit_days == days]
    best = min(a.total_eur for a in airlines) + (min(pickups) if r.door_pickup else 0) + insurance_eur
    if o.total_eur > best * (1 + ACCEPT):
        return [Finding("best_price", WARN, f"{o.total_eur} EUR is {_pct(o.total_eur, best)} above {best} EUR, "
                                            "the cheapest the session had for this request")]  # fmt: skip
    if o.total_eur * (1 + ACCEPT) < best:
        return [Finding("best_price", WARN, f"{o.total_eur} EUR is below every option the session had ({best} EUR)")]
    return []


def _implausible(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """The quote holds no bid `selection.implausible` distrusts (round 67: a trucker's waiting charge per hour
    quoted as the pickup, rejected as too low). `engine.combine` leaves such a bid out; this catches a path that
    did not."""
    return [Finding("implausible_bid", BLOCK, f"{p.trucker or p.carrier} at {p.total_eur} EUR is below {TRUST} times "
                                              "the cheapest other price of its kind")
            for p in distrusted(q, options)][:1]  # fmt: skip


def distrusted(q: Quote, options: list[RateOption]) -> list[RateOption]:
    """The session's options the quote is built from (its airline, its trucker) that `selection.implausible`
    distrusts: what `repair` leaves out."""
    o = q.option
    held = [p for p in options if (o.trucker and p.trucker == o.trucker)
            or (p.trucker is None and (p.source, p.carrier) == (o.source, o.carrier))]  # fmt: skip
    return [p for p in held if implausible(p, allowed(q.rfq, options) or options)]


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
# "weight: 480") is collected with what stands next to it (net, per piece, a piece count on the same line). The priced
# weight passes only when one plain reading of those figures gives it: one figure, one figure times its pieces, the
# sum of all of them, or the lots added up. Every live misread so far fails this test: "480 lbs" priced as 480 kg,
# "4 cartons, each 48 kg" as 48 kg, a lot left out of the sum. It makes no model call and changes no price.

MASS_UNIT = r"(?P<unit>kgs?|kilo(?:gram(?:m|s)?|s)?|lbs?|pounds?|t(?![-\w])|tons?|tonnes?|tonnen)"
MASS = re.compile(
    rf"(?P<num>\d{{1,3}}(?:[ \u00a0\u202f]\d{{3}})+(?![\d.,])|\d[\d.,']*\d|\d)\s*{MASS_UNIT}(?![\w])", re.IGNORECASE
)
MASS_LABEL = re.compile(  # "Gross weight: 480" without a unit is read as kg by the rules; read it the same way here
    r"\b(?:gross\s+weight|weight|gewicht|bruttogewicht)(?:\s*\(kgs?\))?\s*:\s*(?P<num>\d[\d.,']*\d|\d)(?![\d.,']*\s*[a-z])",
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
# Figures that are not the shipment's gross weight: a limit, a capacity, a chargeable or volume weight, what a truck
# carries or a pallet is rated for, an earlier shipment ("last time we shipped 800 kg").
MASS_OTHER = re.compile(
    r"(?:max(?:imum|\.)?|up\s+to|bis\s+zu|limit|capacity|chargeable|volumetric|volume\s+weight|frachtpflichtig\w*"
    r"|volumengewicht|carr(?:y|ies)|rated\s+(?:for|at)|holds?)\s*(?:weight\s*)?(?:of\s+)?:?\s*$"
    r"|\b(?:last\s+(?:time|month|week|year)|previous(?:ly)?|letztes\s+mal)\b[^.\n]*$",
    re.IGNORECASE,
)
MASS_COUNT = re.compile(
    r"(\d+)\s*(?:x\s+)?(?:pcs|pieces?|pallets?|colli|packages?|cartons?|boxes|box|cases?|drums?|crates?|units?|stück"
    r"|stk|paletten|kartons?|kisten?|fässer)\b",
    re.IGNORECASE,
)
MASS_FACTOR = re.compile(r"(\d+)\s*[x×]\s*$", re.IGNORECASE)  # "2 x 300 kg"
TO_KG = {"lb": Decimal("0.45359237"), "t": Decimal(1000)}


@dataclass(frozen=True)
class Mass:
    """One mass figure of the mail: its readings in kg (1.234 may be 1234 or 1.234), and the pieces it is for when
    the mail says it is per piece (None for a figure of the whole lot or shipment; 0 for "each" without a count)."""

    kg: frozenset[Decimal]
    per_piece: int | None
    other: bool  # a limit, capacity or chargeable weight: never required to add up, may still match


def masses(text: str) -> list[Mass]:
    found = []
    for line in text.splitlines():
        if len(line) > LINE_MAX * 2:
            continue
        hits = [(m, m.group("unit")) for m in MASS.finditer(line)]
        hits += [(m, "kg") for m in MASS_LABEL.finditer(line) if not any(h.start() <= m.start("num") < h.end()
                                                                         for h, _ in hits)]  # fmt: skip
        for m, unit in hits:
            before, after = line[: m.start("num")], line[m.end() :]
            if MASS_NET.match(after) or MASS_NET_BEFORE.search(before[-20:]):
                continue
            u = unit.lower()
            factor = TO_KG["lb"] if u.startswith(("lb", "pound")) else TO_KG["t"] if u.startswith("t") else Decimal(1)
            raw = m.group("num")
            parts = {raw, raw.split()[-1]} if raw.split()[1:] else {raw}  # "1 250 kg" is 1250, or "1" then 250
            readings = {v * factor for r in parts for t in (True, False) if (v := number(r, t)) is not None and v > 0}
            if not readings:
                continue
            per_piece = None
            if (f := MASS_FACTOR.search(before)) is not None:
                per_piece = int(f.group(1))
            elif MASS_EACH_BEFORE.search(before[-25:]) or MASS_EACH_AFTER.match(after):
                counts = MASS_COUNT.findall(before)
                per_piece = int(counts[-1]) if counts else 0
            found.append(Mass(frozenset(readings), per_piece, bool(MASS_OTHER.search(before[-30:]))))
    return found


def explanations(figures: list[Mass], listed: bool) -> set[Decimal]:
    """Every weight in kg a plain reading of the figures gives. A mail that lists its shipment line by line
    (`listed`) is explained only by the sum of its lines, or by a total line that equals that sum: a single line
    alone is a lot left out (922 kg priced for 1822 kg). Any other mail by its largest figure (a total stands beside
    the per-piece and part figures) or the sum of all of them."""

    def values(f: Mass) -> set[Decimal]:
        if f.per_piece is None:
            return set(f.kg)
        return {kg * f.per_piece for kg in f.kg} if f.per_piece else set()

    shipment = [f for f in figures if not f.other and values(f)]
    if not shipment:
        return {v for f in figures for v in values(f)}
    low = [min(values(f)) for f in shipment]  # the usual reading: 1.234 kg is 1234 kg, 1,5 t is 1500 kg
    high = [max(values(f)) for f in shipment]
    for i, f in enumerate(shipment):  # a total line: one figure that is the sum of all the others
        rest = sum(low[:i] + low[i + 1 :], Decimal(0))
        if len(shipment) > 2 and any(_close(v, rest) for v in values(f)):
            return {v for v in values(f)}
    out = {sum(low, Decimal(0)), sum(high, Decimal(0))}
    if not listed or len(shipment) == 1:
        out |= {max(low), max(high)}
    return out


LISTED = re.compile(r"^\s*(?:[-*•]|(?:lot|pos(?:ition)?|item|part|teil|los)\s*\.?\s*\d+\s*[:.)-])", re.IGNORECASE)


def listed(text: str) -> bool:
    """Two or more list lines that each carry a weight: the shipment is given line by line."""
    return sum(1 for line in text.splitlines() if LISTED.match(line) and masses(line)) >= 2


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.5"), b * Decimal("0.005"))


def weight_readings(text: str) -> set[Decimal]:
    """Every gross weight in kg a plain reading of `text` gives (`explanations`, the lots added up); empty when it
    writes no weight."""
    figures = masses(text)
    if not figures:
        return set()
    ways = explanations(figures, listed(text))
    if (lots := lot_weight(text)) is not None:
        ways.add(lots)
    return ways


def _accounting(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """The priced weight is one the mail's own figures explain, else BLOCK. A mail that writes no weight at all
    only warns: the weight may come from an attachment the check cannot read."""
    text = own_words(request)
    weight = q.rfq.weight_kg
    figures = masses(text)
    if not figures:  # nothing to hold the weight against: an attachment, a mail we could not load
        return [Finding("weight_unexplained", WARN, f"priced on {weight} kg, the mail writes no weight")]
    ways = weight_readings(text)
    if any(_close(weight, w) for w in ways):
        return []
    if any(_close(weight, part.weight_kg) for part in shipments(q.rfq, text)):  # one of several shipments, each quoted
        return []
    shown = ", ".join(f"{w:g} kg" for w in sorted(ways)[:6]) if ways else "none"
    each = [f for f in figures if f.per_piece == 0]
    hint = "; a weight per piece without a piece count" if each else ""
    return [Finding("weight_unexplained", BLOCK, f"priced on {weight} kg; the mail's figures give {shown}{hint}")]


# --- volume accounting: the priced volume must be one the mail's own figures explain --------------------------------
#
# The mirror of the weight accounting (round 79: "95.00 cubic feet" priced as 95 cbm; rounds 33 to 35: the mail's
# volume over the packing list's). Every volume figure the customer wrote: cbm or m3, cubic feet, litres, and
# dimensions times the pieces on their line. It matters only when the volume decides the chargeable weight.

VOLUME_FIGURE = re.compile(
    r"(?P<num>\d[\d.,']*\d|\d)\s*(?P<unit>cbm\b|m3\b|m³|cubic\s+met\w*|kubikmeter\b|cubic\s+f(?:ee|oo)t\b|cu\.?\s*ft\b"
    r"|ft3\b|ft³|lit(?:er|re)s?\b|ltrs?\b)",
    re.IGNORECASE,
)
VOLUME_LABEL = re.compile(
    r"\b(?:volume|volumen|cbm)(?:\s+(?:gesamt|insgesamt|total))?(?:\s*\((?:cbm|m3|m³)\))?\s*:\s*(?P<num>\d[\d.,']*\d|\d)"
    r"(?![\d.,']*\s*[a-z])",
    re.IGNORECASE,
)
FT3 = Decimal("0.028316846592")
CBM_CLOSE = Decimal("0.01")


def _cbm(raw: str, unit: str) -> Decimal | None:
    value = number(raw, False)
    if value is None or value <= 0:
        return None
    u = unit.lower()
    if "f" in u:
        return value * FT3
    if u.startswith("l"):
        return value / 1000
    return value


def volume_readings(text: str, pieces: int | None = None) -> set[Decimal]:
    """Every volume in cbm a plain reading of `text` gives: each stated figure, a figure per piece times the pieces
    on its line, the sum of all stated figures, each set of dimensions alone, times the pieces on its line and times
    the request's pieces, and the sum of all dimension lines."""
    stated, each, dims_lines = [], [], []
    for line in text.splitlines():
        if len(line) > LINE_MAX * 2:
            continue
        hits = [(m.group("num"), m.group("unit"), m) for m in VOLUME_FIGURE.finditer(line)]
        hits += [(m.group("num"), "cbm", m) for m in VOLUME_LABEL.finditer(line)]
        counts = [int(c) for c in MASS_COUNT.findall(line)]
        for raw, unit, m in hits:
            if (v := _cbm(raw, unit)) is None:
                continue
            before, after = line[: m.start()], line[m.end() :]
            if MASS_EACH_BEFORE.search(before[-25:]) or MASS_EACH_AFTER.match(after):
                each.extend(v * c for c in counts or ([pieces] if pieces else []))
                each.append(v)
            else:
                stated.append(v)
        for m in extract.DIMS.finditer(line):
            *sides, unit = m.groups()
            volume = Decimal(1)
            for side in sides:
                volume *= number(side, thousands=False) / extract._per_m(unit)
            dims_lines.append((volume, counts[0] if counts else None))
    out = set(stated) | set(each)
    if len(stated) > 1:
        out.add(sum(stated, Decimal(0)))
    for volume, count in dims_lines:
        out |= {volume, *(volume * n for n in (count, pieces) if n)}
    if len(dims_lines) > 1:
        out.add(sum((v * (c or 1) for v, c in dims_lines), Decimal(0)))
    return out


def _volume_accounting(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """The priced volume is one the mail's own figures explain when the volume decides the chargeable weight, else
    BLOCK; a volume the mail does not write is the attachment's (WARN when there is one)."""
    r = q.rfq
    text = own_words(request)
    ways = volume_readings(text, r.pieces)
    if any(abs(r.volume_cbm - w) <= max(CBM_CLOSE, w * CBM_CLOSE) for w in ways):
        return []
    drives = max([r.volume_cbm, *ways], default=Decimal(0)) * CBM_KG > r.weight_kg
    if not drives:
        return []
    if not ways:  # nothing to hold the volume against: an attachment, the subject, a default
        return []
    shown = ", ".join(f"{w.quantize(Decimal('0.01'))} cbm" for w in sorted(ways)[:6])
    level = WARN if files else BLOCK  # a packing list may govern: the evidence says so, the check cannot read it
    return [Finding("volume_unexplained", level, f"priced on {r.volume_cbm} cbm; the mail's figures give {shown}")]


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
    if others and len(parts := shipments(q.rfq, own_words(request))) > 1:
        # Round 125: each further shipment's price, then their sum when every one has a price (`compose.shipments`).
        prices, rest = others[: len(parts) - 1], others[len(parts) - 1 :]
        if not rest or rest == [q.total + sum(prices)]:
            others = []
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
    counts = compose._words_lang(q.body)
    lang = compose.language_of(q.rfq)
    reads = max(counts, key=counts.get)
    if reads != lang and counts[reads] > counts[lang] * 2 and counts[reads] >= 8:
        return [Finding("language", WARN, f"customer writes {lang}, the mail reads {reads}")]
    return []


def _excluded(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Round 42: an airline the customer ruled out is rejected as excluded_airline. `selection.select` quotes one
    only when every airline is ruled out (R1); with an allowed airline among the options that is a BLOCK."""
    r, o = q.rfq, q.option
    if not r.excluded_carriers or allowed(r, [o]):
        return []
    others = [
        a for a in allowed(r, options) if a.trucker is None and (a.origin, a.destination) == (r.origin, r.destination)
    ]
    level = BLOCK if others else WARN
    return [Finding("excluded_carrier", level, f"{o.carrier} is ruled out by the customer"
                                               + (f"; {others[0].carrier} is allowed" if others else "; no other airline"))]  # fmt: skip


def _insured(
    q: Quote, request: str, options: list[RateOption], files: tuple[str, ...], *, insurance_eur: Decimal
) -> list[Finding]:
    """Round 91: a request that states its insurance terms is scored with the premium in the all-in (too_low)."""
    if insurance_eur > 0 or not extract.insurance(own_words(request)):
        return []
    return [Finding("insurance_missing", BLOCK, "the request states insurance terms, the all-in holds no premium")]


def _language_sent(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Rounds 70 and 72 (wrong_language): read the language of the customer's own words and of the finished mail,
    after any translation, independently of `rfq.language`. A script the mail does not share (Chinese answered in
    English) blocks; for Latin scripts only a clear lead of the request's common words over every other language."""
    said = own_words(request)
    if extract.ASKED_LANGUAGE.search(extract._unsigned(said)):
        return []  # "please answer in English": the language they ask for, which `extract._language` reads
    body = ours(q.body)
    asked, wrote = extract._script(said), extract._script(body)
    if asked and asked != wrote:
        return [Finding("language_sent", BLOCK, f"the customer writes {asked}, the mail is not in that script")]
    if asked or wrote:
        return []
    counts = extract.words_of(extract._unsigned(said))
    lang = max(counts, key=counts.get)
    second = max((n for other, n in counts.items() if other != lang), default=0)
    if counts[lang] < 6 or counts[lang] < second * 3:
        return []
    ours_counts = compose._words_lang(body)
    reads = max(ours_counts, key=ours_counts.get)
    if reads != lang and ours_counts[reads] >= 8 and ours_counts[reads] > ours_counts.get(lang, 0) * 2:
        return [Finding("language_sent", BLOCK, f"the customer writes {lang}, the mail reads {reads}")]
    return []


def _offer_pdf(q: Quote, request: str, options: list[RateOption], files: tuple[str, ...]) -> list[Finding]:
    """Rounds 76 and 78 (no_pdf_offer): the PDF `outbox.send_quote` attaches must be whole and hold the total."""
    data = pdf.offer(q)
    if not data.startswith(b"%PDF") or not data.rstrip().endswith(b"%%EOF"):
        return [Finding("offer_pdf", BLOCK, "the offer PDF is not a whole PDF file")]
    if compose.money(q.total).encode() not in data:
        return [Finding("offer_pdf", BLOCK, f"the offer PDF does not hold the total {compose.money(q.total)}")]
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


RULES = (_required, _lane, _arithmetic, _chargeable, _pickup, _best_price, _implausible, _per_kg, _weight, _accounting,
         _dangerous, _text, _language, _validity, _excluded, _insured, _language_sent, _offer_pdf, _volume_accounting)  # fmt: skip
