"""Bid requests to carriers by mail and their answers as RateOptions. Rules read the answer; the model only
transcribes an answer the rules cannot read, and code does every sum."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx

from pixel import llm
from pixel.api.client import ApiError
from pixel.contracts import Message, RateOption, Rfq, Source
from pixel.core import events, outbox, timing
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import calc, session, store

POLL_S = 0.5
# Grouped thousands only when a separator is there: an ungrouped "1195.90" is one amount, not "119". The Swiss group
# with an apostrophe: "CHF 2'748.45" is no "CHF 2".
AMOUNT = r"(?:\d{1,3}(?:[ \u00a0'’]\d{3})+|\d+)(?:[.,]\d+)*"
# ISO 4217. Any other three capitals after a number is no price: a time zone, an airport, a unit.
CURRENCY = (
    "EUR|USD|GBP|CHF|JPY|CNY|HKD|SGD|AUD|CAD|NZD|SEK|NOK|DKK|PLN|CZK|HUF|TRY|AED|SAR|QAR|ILS|INR|KRW|TWD|THB|MYR|"
    "IDR|PHP|VND|ZAR|EGP|KES|NGN|BRL|MXN|ARS|CLP|COP|PEN"
)
SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "euro": "EUR", "euros": "EUR"}
PRICED = re.compile(
    rf"(?<![A-Za-z])(?P<c1>{CURRENCY}|[€$£])\s?(?P<n1>{AMOUNT})|(?P<n2>{AMOUNT})\s?(?P<c2>{CURRENCY}|[€$£]|[Ee]uros?)(?![A-Za-z])"
)
# A pdf wraps "for 256.85" and "EUR." onto two lines: amount and currency are joined again, only when the next line
# holds the currency alone ("Rate per kg: 2.80" over "EUR 300.00 all-in" stays two prices).
UNWRAP = re.compile(
    rf"(?<=[.,]\d\d)[ \t]*\n[ \t]*(?=(?:{CURRENCY}|[€$£])(?![A-Za-z])[ \t]*[.,;:!]?[ \t]*(?:\n|$))"
    rf"|(?<![A-Za-z])({CURRENCY}|[€$£])[ \t]*\n[ \t]*(?=\d[\d.,]*[.,]\d\d(?!\d))"
)
TOTAL_LABEL = r"\b(?:total(?: price)?|all[- ]in|gesamt(?:preis)?|overall)\b"
TOTAL_BEFORE = re.compile(
    TOTAL_LABEL + r"(?:\s+(?:freight|air|rate|price|cost|charge|is|of|amount))*[\s:=,-]*$", re.IGNORECASE
)
TOTAL_AFTER = re.compile(r"^\s*(?:in\s+total|all[- ]in|gesamt)\b", re.IGNORECASE)
# An offer letter: "Our price for this transport is 273.20 EUR. It has a base charge of 104.00 EUR and ...", a mail:
# "Our all-in air freight rate from FRA to KTM is ...", "... and deliver it to FRA airport for 182.62 EUR"; a fuel,
# security or waiting rate is a surcharge, never the offer.
PRICE_IS = re.compile(
    r"(?:\b(?:our|the|this)\s+(?!(?:[\w-]+\s+){0,3}?(?:fuel|security|waiting|storage|handling|surcharge)\b)"
    r"(?:[\w-]+\s+){0,3}?(?:price|rate)\b[^\d\n.]{0,40}?\b(?:is|amounts\s+to|comes\s+to)"
    r"|\bdeliver\b[^\d\n.]{0,40}?\bfor|\bwe\s+(?:can\s+)?(?:offer|quote)(?:\s+you)?|\bour\s+offer\s+is)\s*$",
    re.IGNORECASE,
)
# A price beside the offer that is not its own: "rates from", "ab", a minimum, an earlier offer, an option, "... extra".
SIDE_BEFORE = re.compile(
    r"\b(?:from|ab|starting\s+(?:at|from)|as\s+low\s+as|minimum(?:\s+charge)?|min\.|was|would\s+be|optional\s+[\w-]+)"
    r"[\s:]*$",
    re.IGNORECASE,
)
SIDE_AFTER = re.compile(r"^\s*(?:extra|more)\b", re.IGNORECASE)
# An offer table names its currency once, in the header ("Amount (EUR)"), and its total line has none. The nearest
# header above the total counts: "Chargeable weight (kg)" or "per kg (EUR)" over it is no price in that currency.
HEADER_UNIT = re.compile(rf"\((?P<u>{CURRENCY}|[€$£]|kgs?|lbs?|cbm|m3|pcs)\)", re.IGNORECASE)
# The total row as offers write it: "Total", "Grand total", "Sum", "Total amount", "Total EUR   104.62".
TOTAL_LINE = re.compile(
    rf"^\s*(?:grand\s+)?(?:{TOTAL_LABEL}(?:\s+(?:amount|sum))?|sum|summe)(?:\s+(?P<c>{CURRENCY}))?[\s:,-]*(?P<n>{AMOUNT})\s*$",
    re.IGNORECASE,
)
KG_BASIS = r"(?:/\s*|(?:per|pro)\s+)(?:chargeable\s+)?(?:kg|kilograms?|kilogramm)\b"
PER_KG = re.compile(r"^\s*" + KG_BASIS, re.IGNORECASE)
KG_LABEL = re.compile(KG_BASIS + r"\s*(?:is\s*|[=:]\s*)?$", re.IGNORECASE)
# A basis other than the whole shipment ("per pallet", "/ 100 kg"); "per shipment" or "per AWB" is the flat total.
FLAT = r"(?!(?:shipment|consignment|awb|air\s*waybill|sendung|auftrag)\b)"
# "45.00 EUR for each hour" of waiting is a surcharge per unit, never the offer's price.
OTHER_BASIS = re.compile(
    rf"^\s*(?:/\s*{FLAT}(?:\d|[a-z])|(?:per|pro|je|(?:for\s+)?(?:each|every)|an?)\s+{FLAT}(?:\d|[a-z]))", re.IGNORECASE
)
TRANSIT = re.compile(
    r"(\d+)(?:\s*[-–]\s*\d+)?\s*(?:(?:calendar|working|business|werk)\s*)?(days?|d|tage?n?|werktage?n?)\b",
    re.IGNORECASE,
)
# "offer valid for 7 days, transit 3 days": the figure after the transit word counts, a validity never does.
# "Laufzeit des Angebots" is the offer's validity and "transit via DXB" names a hub, neither is a transit time.
TRANSIT_LABEL = r"\b(?:transit(?:\s*time|zeit)?|laufzeit|t/t)\b(?!\s+(?:via\b|de[sr]\s+(?:angebots|offerte)))"
LABELLED_TRANSIT = re.compile(TRANSIT_LABEL + r"[^\d\n]{0,24}?" + TRANSIT.pattern, re.IGNORECASE)
VALIDITY = re.compile(
    r"\b(?:valid(?:ity)?|g(?:ü|ue)ltig\w*|good|laufzeit\s+de[sr]\s+(?:angebots|offerte))\b[^\d\n]{0,20}$", re.IGNORECASE
)
TRANSIT_HOURS = re.compile(TRANSIT_LABEL + r"[^\d\n]{0,24}(\d+)\s*(?:hours?|hrs?|h|stunden?|std)\b", re.IGNORECASE)
# "10.5 hours" and "4,5 Stunden" are one figure; "09:00 h" is a clock time, not a duration.
HOURS = re.compile(r"(?<![\d:.,])(\d+(?:[.,]\d+)?)\s*(?:hours?|hrs?|h\b|stunden?|std\b)", re.IGNORECASE)
PICKUP_HOURS = re.compile(
    r"\b(?:pick\s*up(?:\s+time)?|collect(?:ion)?|abholung)\s*:?\s*(?:within\s+|in\s+)?" + HOURS.pattern,
    re.IGNORECASE,
)
VALIDITY_HOURS = re.compile(r"\bvalid\s+for\s+" + HOURS.pattern, re.IGNORECASE)
FX = re.compile(r"1\s*([A-Z]{3})\s*=\s*(\d+[.,]\d+)\s*EUR|(\d+[.,]\d+)\s*EUR\s*(?:per|/)\s*(?:1\s*)?([A-Z]{3})")
DESK_LINE = re.compile(r"^\s*[-*•]\s*(?P<name>.+?)\s*\((?P<code>[A-Z0-9]{2,3})\):\s*(?P<rest>.+)$", re.MULTILINE)
RATE_ID = re.compile(r"rate_id\s+(\S+?)[,;]?(?:\s|$)")
# The desk's list as a table: columns found by their header, in this order, so "Rate id" is no price column.
DESK_COLUMNS = {
    "rate_id": r"\brate[ _-]?id\b",
    "valid": r"\bvalid|\bg(?:ü|ue)ltig",
    "transit": r"\btransit|\blaufzeit",
    "code": r"\b(?:code|iata)\b",
    "name": r"\b(?:airline|carrier|fluggesellschaft)\b",
    "price": r"\b(?:price|rate|total|all[- ]in|amount|preis)\b",
}
# A cell of each column in a row whose cells are one space apart (a pdf table): the header gives their order.
DESK_CELLS = {
    "rate_id": r"\S+",
    "valid": r"\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}(?: UTC)?",
    "transit": r"\d{1,2}(?:\s*days?)?",
    "code": r"[A-Z0-9]{2,3}",
    "name": r".+?",
    "price": rf"(?:{CURRENCY}|[€$£])?\s?{AMOUNT}(?:\s?(?:{CURRENCY}|[€$£]))?",
}
VALID_UNTIL = re.compile(r"valid until\s+(\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}) UTC")
DECLINE = re.compile(
    r"\b(?:cannot offer|can't offer|unable to|no capacity|not able to|decline|regret|do not serve|don't serve)\b",
    re.IGNORECASE,
)
# "We do not serve this lane/zone: FRA-JFK. We fly CGN-EZE, DUS-JFK, ...": an airline declining a lane lists every
# lane it flies. The setup changes hourly and nothing tells us when, so a learned list counts for an hour.
LANES_FRESH = timedelta(hours=1)
LANE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})\s?(?:[-–—/>→]|\bto\b)\s?([A-Za-z]{3})(?![A-Za-z])")
NOT_SERVED = re.compile(
    r"\bnot (?:serve|served|fly|flown|operate|operated|cover|covered)\b|\b(?:don't|doesn't|can't|cannot) "
    r"(?:serve|fly|operate|cover)\b",
    re.IGNORECASE,
)
# The list runs to the end of its sentence or paragraph: a lane in the next sentence is not one it flies.
FLIES = re.compile(
    r"\bwe (?:only )?(?:fly|serve|operate|cover)\b(.*?)(?:\.(?=\s|$)|\n\s*\n|$)", re.IGNORECASE | re.DOTALL
)
PARTIAL = re.compile(
    r"\b(?:among(?:st)? others|e\.g\.|for example|such as|including|etc)\b|\band more\b|…|\.\.\.", re.IGNORECASE
)


def _lanes(text: str) -> set[str]:
    return {f"{a}-{b}".upper() for a, b in LANE.findall(text)}


def offered_lanes(body: str) -> list[str] | None:
    """The lanes an airline that declines ours says it flies, by rules. None in doubt: no decline, no list, a list
    that says it is incomplete or that holds a lane its decline names."""
    text = _own_text(body)
    if not (no := NOT_SERVED.search(text)) or not (flies := FLIES.search(text)) or PARTIAL.search(flies.group()):
        return None
    start = max(text.rfind("\n", 0, no.start()), text.rfind(". ", 0, no.start())) + 1
    end = min((i for i in (text.find("\n", no.end()), text.find(". ", no.end())) if i >= 0), default=len(text))
    lanes = _lanes(flies.group(1))
    return sorted(lanes) if lanes and not lanes & _lanes(text[start:end]) else None


def learned_lanes(ctx: Context) -> dict[str, set[str]]:
    """Per airline (casefolded name) the lanes of its newest list learned within LANES_FRESH."""
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE kind='carrier.lanes' AND ts >= ? ORDER BY id",
        ((timing.now() - LANES_FRESH).isoformat(),),
    )
    return {(p := json.loads(r[0]))["carrier"].casefold(): set(p["lanes"]) for r in rows}


def worth_asking(ctx: Context, rfq: Rfq, carriers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The airlines without a fresh list that leaves our lane out: unknown or stale ones are asked."""
    known, lane = learned_lanes(ctx), f"{rfq.origin}-{rfq.destination}"
    for name, _ in carriers:
        if lane not in known.get(name.casefold(), {lane}):
            events.emit(ctx, "bid.skipped", "pricing", session_id=rfq.session_id,
                        payload={"carrier": name, "origin": rfq.origin, "destination": rfq.destination})  # fmt: skip
    return [(name, email) for name, email in carriers if lane in known.get(name.casefold(), {lane})]


def _amount(text: str, *, unit_rate: bool = False) -> Decimal:
    if not (m := re.search(AMOUNT, text)):
        raise ValueError(f"no amount in {text!r}")
    text = re.sub(r"[\s'’]", "", m.group())
    if "." in text and "," in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        text = text.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        head, _, tail = text.rpartition(sep)
        text = head.replace(sep, "") + ("." + tail if unit_rate or len(tail) != 3 else tail)
    return Decimal(text)


def _own_text(body: str) -> str:
    """The carrier's own words: quoted lines and our request below are dropped."""
    lines = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^\s*(?:on .+ wrote:|-----\s*original message)", line, re.IGNORECASE):
            break
        lines.append(line)
    return "\n".join(lines)


def _pickup_hours(body: str) -> int | None:
    text = _own_text(body)
    match = PICKUP_HOURS.search(text) or HOURS.search(VALIDITY_HOURS.sub("", text))
    if not match:
        return None
    return int(Decimal(match.group(1).replace(",", ".")).to_integral_value(ROUND_HALF_UP))  # a whole hour (contract)


def _rate(text: str) -> Decimal:
    """Unit rates and FX quotes retain every fractional digit, including trailing zeroes."""
    return Decimal(text.strip().replace(",", "."))


def _price(text: str, rfq: Rfq) -> tuple[Decimal, str] | None:
    found, labelled = set(), set()
    unsure = False  # a charge per unit, or a total without its currency: only a labelled price counts
    header = None  # "": the nearest header names a unit that is no currency
    text = UNWRAP.sub(lambda m: f"{m.group(1) or ''} ", text)
    for line in FX.sub("", text).splitlines():
        if units := {SYMBOLS.get(m.group("u"), m.group("u").upper()) for m in HEADER_UNIT.finditer(line)}:
            money = units & set(CURRENCY.split("|"))
            header = money.pop() if len(money) == 1 and not re.search(KG_BASIS, line, re.IGNORECASE) else ""
        currency = (m.group("c") or "").upper() or header if (m := TOTAL_LINE.match(line)) else ""
        if m and currency is None:  # no header names its unit
            unsure = True
        elif m and currency:
            price = (calc.bid_charge(_amount(m.group("n")), False, rfq), currency)
            found.add(price)
            labelled.add(price)
        prices = list(PRICED.finditer(line))
        for i, m in enumerate(prices):
            before = line[prices[i - 1].end() if i else 0 : m.start()]
            after = line[m.end() : prices[i + 1].start() if i + 1 < len(prices) else len(line)]
            per_kg = bool(PER_KG.match(after) or KG_LABEL.search(before))
            if not per_kg and OTHER_BASIS.match(after):
                unsure = True
                continue
            if SIDE_BEFORE.search(before) or SIDE_AFTER.match(after):
                continue
            raw = m.group("n1") or m.group("n2")
            amount = _amount(raw, unit_rate=per_kg)
            currency = m.group("c1") or m.group("c2")
            price = (calc.bid_charge(amount, per_kg, rfq), SYMBOLS.get(currency.lower(), currency))
            found.add(price)
            if TOTAL_BEFORE.search(before) or TOTAL_AFTER.match(after) or PRICE_IS.search(before):
                labelled.add(price)
    if unsure and not labelled:
        return None
    candidates = labelled or found
    return next(iter(candidates)) if len(candidates) == 1 else None


def _days(text: str) -> int | None:
    if match := TRANSIT_HOURS.search(text):
        return calc.transit_days(int(match[1]), hours=True)
    # Two labelled transit times ("our rate to CAI last week ... transit 5 days"): which is this offer's is unclear.
    if labelled := {int(m[1]) for m in LABELLED_TRANSIT.finditer(text)}:
        return calc.transit_days(labelled.pop(), hours=False) if len(labelled) == 1 else None
    for match in TRANSIT.finditer(text):
        if not VALIDITY.search(text[: match.start()]):
            return calc.transit_days(int(match[1]), hours=False)
    return None


def _fx(text: str, currency: str) -> Decimal | None:
    for m in FX.finditer(text):
        code, rate = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if code == currency:
            return _amount(rate, unit_rate=True)
    return None


def _option(rfq: Rfq, message: Message, total: Decimal, currency: str, fx: Decimal | None, days: int) -> RateOption:
    chargeable = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    total = calc.money(total)
    return RateOption(
        source=Source.BID,
        carrier=message.sender,
        origin=rfq.origin,
        destination=rfq.destination,
        chargeable_kg=chargeable,
        billed_kg=chargeable,
        freight=total,
        total=total,
        currency=currency,
        fx_eur=None if currency == "EUR" else fx,
        total_eur=calc.to_eur(total, currency, fx),
        transit_days=days,
        evidence_message_id=message.id,
    )


def parse(message: Message, rfq: Rfq, *, needs_transit: bool = True) -> RateOption | None:
    """Price, currency and transit time from a carrier's answer, by rules. None when the rules cannot read it."""
    text = _own_text(message.body)
    if DECLINE.search(text) or not (price := _price(text, rfq)):
        return None
    total, currency = price
    fx = None if currency == "EUR" else _fx(text, currency)
    days = _days(text)
    if total <= 0 or (currency != "EUR" and (fx is None or fx <= 0)) or (needs_transit and days is None):
        return None
    return _option(rfq, message, total, currency, fx, days or 0)


def _desk_rows(text: str) -> str:
    """A table of the desk's rates (cells two spaces apart, as `tariffs.agent._lines` joins them) written as the
    desk's own lines, "- Name (CODE): 952.00 EUR, 4 days, rate_id ..., valid until ... UTC". A row whose cells do
    not line up with the header is left out."""
    rows, header, needed = [], None, {"name", "code", "price", "transit", "rate_id"}
    for line in text.splitlines():
        cells = [c for c in re.split(r"\s{2,}|\t", line.strip()) if c]
        found = {}
        for i, cell in enumerate(cells):
            key = next((k for k, pattern in DESK_COLUMNS.items() if re.search(pattern, cell, re.IGNORECASE)), None)
            if key and key not in found:
                found[key] = i
        spaced = {}  # one space between the cells: each column's first header word, by its place in the line
        for key, pattern in DESK_COLUMNS.items():
            taken = [(s, e) for s, e in spaced.values()]
            if m := next((m for m in re.finditer(pattern, line, re.IGNORECASE)
                          if not any(s <= m.start() < e for s, e in taken)), None):  # fmt: skip
                spaced[key] = (m.start(), m.end())
        if needed <= found.keys() or (len(cells) == 1 and needed <= spaced.keys()):
            price_header = cells[found["price"]] if needed <= found.keys() else line[spaced["price"][0] :]
            currency = re.match(rf"[^()\n]*?\((?:({CURRENCY})|([€$£]))\)", price_header)
            order = sorted(spaced, key=lambda k: spaced[k][0])
            row = re.compile(r"^\s*" + r"\s+".join(f"(?P<{k}>{DESK_CELLS[k]})" for k in order) + r"\s*$")
            header = (len(cells), found, currency and (currency[1] or SYMBOLS[currency[2]]), row)
            continue
        if header is None:
            continue
        if needed <= header[1].keys() and len(cells) == header[0]:
            cell = {k: cells[i] for k, i in header[1].items()}
        elif len(cells) == 1 and (m := header[3].match(line)):
            cell = m.groupdict()
        else:
            continue
        price = cell["price"] if PRICED.search(cell["price"]) or not header[2] else f"{cell['price']} {header[2]}"
        transit = cell["transit"] if re.search(r"[a-z]", cell["transit"], re.IGNORECASE) else f"{cell['transit']} days"
        valid = f", valid until {cell['valid'].removesuffix(' UTC')} UTC" if "valid" in cell else ""
        rows.append(f"- {cell['name']} ({cell['code']}): {price}, {transit}, rate_id {cell['rate_id']}{valid}")
    return "\n".join(rows)


def parse_desk(message: Message, rfq: Rfq) -> list[RateOption]:
    """The rate desk lists the portal's rates for a lane, one airline per line, each with its rate id, or as a table
    in an attachment (`_desk_rows`). Lines without a price (an airline that is not on the portal) are skipped."""
    found = []
    text = _own_text(message.body)
    for line in DESK_LINE.finditer(text if DESK_LINE.search(text) else _desk_rows(text)):
        rest = line["rest"]
        price, transit, rate_id = _price(rest, rfq), _days(rest), RATE_ID.search(rest)
        if not (price and transit and rate_id):
            continue
        total, currency = price
        fx = None if currency == "EUR" else _fx(rest, currency)
        if currency != "EUR" and fx is None:
            continue
        until = VALID_UNTIL.search(rest)
        option = _option(rfq, message, total, currency, fx, transit)
        found.append(
            option.model_copy(
                update={
                    "source": Source.RATEDESK,
                    "carrier": line["name"],
                    "carrier_code": line["code"],
                    "rate_id": rate_id.group(1),
                    "valid_until": until and datetime.strptime(until.group(1), "%d %b %Y %H:%M").replace(tzinfo=UTC),
                }
            )
        )
    return found


ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["offer", "total", "components", "currency", "transit_days", "eur_per_unit"],
    "properties": {
        "offer": {"type": "boolean", "description": "false when the carrier declines or gives no price"},
        "total": {"type": ["string", "null"], "description": "the all-in total exactly as written, if stated"},
        "components": {"type": "array", "items": {"type": "string"}, "description": "each charge as written"},
        "currency": {"type": ["string", "null"]},
        "transit_days": {"type": ["string", "null"]},
        "eur_per_unit": {"type": ["string", "null"], "description": "exchange rate to EUR, only if stated"},
    },
}

ANSWER_PROMPT = (
    "A carrier answered our request for a freight price. Copy what the mail states, numbers exactly as written; "
    "never compute, convert or guess. Copy the units of amounts (/kg, flat) and time (hours, days). "
    "If only separate charges are given, list them in components and leave total "
    "null."
)


async def _read_with_model(ctx: Context, message: Message, rfq: Rfq, needs_transit: bool) -> RateOption | None:
    result = await llm.ask_json(
        ctx,
        purpose="bid.read",
        system=ANSWER_PROMPT,
        user=_own_text(message.body)[:4000],
        schema=ANSWER_SCHEMA,
        session_id=message.session_id,
        max_output_tokens=150,
    )
    data = result.data
    if not data["offer"] or not data["currency"]:
        return None
    charges = [data["total"]] if data["total"] else data["components"]
    parts = []
    for charge in charges:
        token = re.search(AMOUNT, charge)
        if not token:
            return None
        # The schema returns a verbatim charge, with its currency optionally between amount and unit.
        tail = re.sub(rf"^\s*(?:{CURRENCY}|[€$£])", "", charge[token.end() :])
        per_kg = bool(PER_KG.match(tail) or KG_LABEL.search(charge[: token.start()]))
        if not per_kg and OTHER_BASIS.match(tail):
            return None
        amount = _amount(token[0], unit_rate=per_kg)
        parts.append((amount, per_kg))
    total = calc.bid_total(parts, rfq)
    currency = SYMBOLS.get(data["currency"].strip(), data["currency"].strip().upper())
    fx = _amount(data["eur_per_unit"], unit_rate=True) if data["eur_per_unit"] else None
    duration = data["transit_days"] or ""
    transit = _days(f"Transit {duration}") or (int(duration) if duration.isdigit() else None)
    if total <= 0 or (currency != "EUR" and fx is None) or (needs_transit and not transit):
        return None
    return _option(rfq, message, total, currency, fx, transit or 0)


CHECK_WAIT_S = 6


async def _crosscheck(
    ctx: Context, message: Message, rfq: Rfq, option: RateOption, *, needs_transit: bool
) -> tuple[RateOption, str]:
    """The rules' reading of an answer checked by the model's (`_read_with_model`, code adds the charges up). Where the
    totals differ the model's wins: in round 67 the rules read a waiting-time line (45.00 EUR) as the offer, the model
    read 181.96; on 359 answers of rounds 9 to 72 both read the same. On any failure the rules' reading stands."""
    try:
        read = await asyncio.wait_for(_read_with_model(ctx, message, rfq, needs_transit), CHECK_WAIT_S)
    except Exception as exc:  # noqa: BLE001  the rules' reading stands
        events.emit(ctx, "error", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"where": "bid.crosscheck", "error": type(exc).__name__})  # fmt: skip
        return option, "rules"
    if read is None or (read.total, read.currency) == (option.total, option.currency):
        return option, "rules"
    events.emit(ctx, "bid.crosscheck", "pricing", session_id=message.session_id, message_id=message.id,
                payload={"rules": f"{option.total} {option.currency}", "model": f"{read.total} {read.currency}"})  # fmt: skip
    return read, "model"


def _postcode(rfq: Rfq) -> str:
    """Truckers price by zone but quote only with a five-digit postcode ("To quote we need: the pickup postcode"):
    a pickup known by its town alone is asked with the zone's first one."""
    return rfq.pickup_postcode or (rfq.pickup_zone or "").ljust(5, "0")


def _subject(rfq: Rfq, pickup: bool) -> str:
    if pickup:
        return f"Pickup request {_postcode(rfq)} to {rfq.origin}"
    return f"Rate request {rfq.origin}-{rfq.destination}, {rfq.weight_kg} kg"


def _body(rfq: Rfq, pickup: bool) -> str:
    chargeable = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    shipment = f"{rfq.weight_kg} kg gross, {rfq.volume_cbm} cbm" + (f", {rfq.pieces} pieces" if rfq.pieces else "")
    if pickup:
        return (
            "Hello,\n\nplease quote a pickup for an air export shipment:\n"
            f"From: postcode {_postcode(rfq)} (zone {rfq.pickup_zone})\nTo: {rfq.origin} airport\n"
            f"Shipment: {shipment}\n\nPlease reply with your price and currency.\n\nThank you,\nPIXEL Air Freight"
        )
    return (
        "Hello,\n\nplease quote air freight for this shipment:\n"
        f"From: {rfq.origin}\nTo: {rfq.destination}\nShipment: {shipment}, chargeable weight {chargeable} kg\n\n"
        "Please reply with your all-in price (freight, fuel and security) with currency, and the transit time in "
        "days.\n\nThank you,\nPIXEL Air Freight"
    )


async def request(ctx: Context, rfq: Rfq, carriers: list[tuple[str, str]], *, pickup: bool = False) -> list[int]:
    """One bid request per carrier (airline, trucker or the rate desk). Returns the sent mail ids; the poller
    maps the answers back to the session through them."""

    async def one(name: str, email: str) -> int | None:
        try:
            sent = await outbox.send(
                ctx,
                to=email,
                subject=_subject(rfq, pickup),
                body=_body(rfq, pickup),
                session_id=rfq.session_id,
                purpose="bid_pickup" if pickup else "bid_request",  # the router places answers by the "bid" prefix
            )
        except outbox.NotSent:
            return None
        except (ApiError, httpx.HTTPError) as exc:
            events.emit(ctx, "option.failed", "pricing", session_id=rfq.session_id,
                        payload={"source": "mail", "carrier": name, "error": type(exc).__name__})  # fmt: skip
            return None
        events.emit(
            ctx,
            "bid.requested",
            "pricing",
            session_id=rfq.session_id,
            message_id=sent.id,
            payload={
                "carrier": name,
                "email": email,
                "pickup": pickup,
                "origin": rfq.origin,
                "destination": rfq.destination,
            },
        )
        return sent.id

    sent = await asyncio.gather(*(one(name, email) for name, email in carriers))
    return [message_id for message_id in sent if message_id is not None]


def _enquiry(ctx: Context, message: Message, rfq: Rfq) -> tuple[int | None, bool]:
    """Identify the enquiry being answered before pricing the reply with the current RFQ.
    Older request events can recover their lane from the extraction preceding that enquiry."""
    rows = ctx.db.execute(
        "SELECT e.message_id, e.payload, m.subject, "
        "(SELECT r.payload FROM events r WHERE r.session_id=e.session_id AND r.kind='rfq.extracted' "
        "AND r.id<=e.id ORDER BY r.id DESC LIMIT 1) AS previous FROM events e "
        "LEFT JOIN messages m ON m.id=e.message_id WHERE e.session_id=? AND e.kind='bid.requested' ORDER BY e.id DESC",
        (rfq.session_id,),
    ).fetchall()
    current = (rfq.origin, rfq.destination)
    refs = [message.in_reply_to, *reversed(message.references)]
    by_ref, by_sender, by_subject = {}, [], []
    for row in rows:
        data = json.loads(row["payload"])
        facts = data if "origin" in data else json.loads(row["previous"] or "{}")
        item = (row["message_id"], (facts.get("origin"), facts.get("destination")))
        by_ref[item[0]] = item
        if data.get("email", "").casefold() == message.sender.casefold():
            by_sender.append(item)
            subject = (row["subject"] or "").casefold()
            if subject and subject in f"{message.subject}\n{message.body}".casefold():
                by_subject.append(item)
    for ref in refs:
        if ref in by_ref:
            enquiry_id, lane = by_ref[ref]
            return enquiry_id, lane == current
    # Standalone replies can identify the request by subject or a sender with a single requested lane.
    candidates = by_subject or by_sender
    if candidates:
        return candidates[0][0], all(lane == current for _, lane in candidates)
    lanes = ctx.db.execute(
        "SELECT DISTINCT json_extract(payload,'$.origin'), json_extract(payload,'$.destination') "
        "FROM events WHERE kind='rfq.extracted' AND session_id=?",
        (rfq.session_id,),
    ).fetchall()
    return None, all(tuple(lane) == current for lane in lanes)


async def _with_attachments(ctx: Context, message: Message) -> tuple[str, list[str]]:
    """The carrier's own words and the text of every attachment code can read: truckers send their offer as a pdf
    or docx and write only "see the attachment" in the mail. Also the files code cannot read (a video, a scan)."""
    from pixel.agents.tariffs.agent import attachment_lines

    parts, unread = [_own_text(message.body)], []
    for attachment in message.attachments:
        if (lines := await attachment_lines(ctx, message, attachment, "pricing")) is None:
            unread.append(attachment.filename)
        else:
            parts += lines
    return "\n".join(parts), unread


async def record(ctx: Context, message: Message) -> None:
    """Called by the orchestrator for every bid or desk answer: parse (model only if `parse` gives None),
    emit `bid.received` and `option.found`."""
    rfq = session.rfq(ctx, message.session_id)
    if rfq is None:
        events.emit(ctx, "error", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"error": "bid for a session without an RFQ"})  # fmt: skip
        return
    name, kind = store.carrier_by_email(ctx, message.sender) or (message.sender, "ratedesk")
    if kind == "airline" and (lanes := offered_lanes(message.body)):
        events.emit(ctx, "carrier.lanes", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"carrier": name, "lanes": lanes})  # fmt: skip
    pickup = kind == "trucker"
    enquiry, current = _enquiry(ctx, message, rfq)
    if not current:
        events.emit(ctx, "bid.received", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"carrier": name, "pickup": pickup, "priced": False, "request_id": enquiry,
                             "ignored": "different or ambiguous lane"})  # fmt: skip
        return

    def by_rules(message: Message) -> tuple[list[RateOption], RateOption | None]:
        listed = parse_desk(message, rfq) if kind == "ratedesk" else []
        return listed, None if listed else parse(message, rfq, needs_transit=not pickup)

    # An attachment counts only when the mail alone holds no price: an airline's terms or a forwarded invoice beside
    # a priced mail must not hide that price.
    listed, option = by_rules(message)
    unread: list[str] = []
    if message.attachments and not listed and option is None:
        body, unread = await _with_attachments(ctx, message)
        message = message.model_copy(update={"body": body})
        listed, option = by_rules(message)
    read_by = "rules"
    if option is not None and ctx.settings.model_check:
        option, read_by = await _crosscheck(ctx, message, rfq, option, needs_transit=not pickup)
    # The desk writes in one fixed layout: what its rules cannot read holds no rate, and costs no model call. An offer
    # only in a file code cannot read leaves no price in the text for a model to copy either.
    if (
        not listed
        and option is None
        and kind != "ratedesk"
        and not DECLINE.search(_own_text(message.body))
        and not (unread and not PRICED.search(message.body))
    ):
        read_by = "model"
        try:
            option = await _read_with_model(ctx, message, rfq, needs_transit=not pickup)
        except Exception as exc:  # noqa: BLE001  one unreadable answer must not stop the others
            events.emit(ctx, "error", "pricing", session_id=message.session_id, message_id=message.id,
                        payload={"where": "bid.read", "error": type(exc).__name__})  # fmt: skip
    if option is not None:
        if pickup:
            option = option.model_copy(update={"freight": Decimal(0), "pickup": option.total, "trucker": name,
                                               "pickup_hours": _pickup_hours(message.body)})  # fmt: skip
        elif kind == "ratedesk":
            option = option.model_copy(update={"source": Source.RATEDESK})
        listed = [option.model_copy(update={"carrier": name})]
    events.emit(
        ctx,
        "bid.received",
        "pricing",
        session_id=message.session_id,
        message_id=message.id,
        payload={
            "carrier": name,
            "pickup": pickup,
            "priced": bool(listed),
            "read_by": read_by,
            "request_id": enquiry,
            "origin": rfq.origin,
            "destination": rfq.destination,
        }
        | ({"unread": unread} if unread else {}),
    )
    for option in listed:
        events.emit(ctx, "option.found", "pricing", session_id=message.session_id, message_id=message.id,
                    payload={"option": option.model_dump(mode="json")})  # fmt: skip


async def collect(
    ctx: Context, session_id: int, deadline: Deadline, expected: list[int] | int, *, pickup: bool = False
) -> list[RateOption]:
    """Give the current lane's enquiries their response window after mailing, up to the hard deadline."""
    rfq = session.rfq(ctx, session_id)
    if rfq is None:
        return []
    after_ask = timing.now() + timedelta(seconds=ctx.settings.bid_wait_s)
    until = min(max(deadline.target_at, after_ask), deadline.hard_at)
    while expected and timing.now() < until:
        rows = ctx.db.execute(
            "SELECT json_extract(payload,'$.request_id'), json_extract(payload,'$.carrier') FROM events "
            "WHERE session_id=? AND kind='bid.received' AND json_extract(payload,'$.origin')=? "
            "AND json_extract(payload,'$.destination')=? AND json_extract(payload,'$.pickup')=?",
            (session_id, rfq.origin, rfq.destination, pickup),
        ).fetchall()
        if isinstance(expected, int):
            if len({row[1] for row in rows}) >= expected:
                break
        elif set(expected).issubset(row[0] for row in rows):
            break
        await asyncio.sleep(min(POLL_S, max(0.0, (until - timing.now()).total_seconds())))
    return [
        o
        for o in session.for_request(ctx, rfq)
        if o.source in (Source.BID, Source.RATEDESK) and (o.trucker is not None) == pickup
    ]
