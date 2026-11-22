"""Generated requests, read offline by `extract.read_request`: a whole class of wording or document read wrong shows
here before a round is lost to it. Every case is built from known figures and its answer is computed here, never
by the code under test.

uv run python -m tools.simulate            # per axis: read right, left to the model, read wrong
uv run python -m tools.simulate --show 10  # the first wrong cases outside the known gaps, in full
uv run python -m tools.simulate --seed 3   # the same grid with other figures; a failing seed becomes a fixture

Three families. `document`: a mail with an attached packing list, letter of instruction or specification (PDF, xlsx,
docx, txt, csv) in the layouts seen since round 33 and their neighbours. Which source a right reading takes depends
on how mail and document relate (`relation`): `tests/fixtures/rfqs/simulate-sources.json` says it per relation and
why, so a correction the customer makes or an attachment for another shipment is never read as "the document wins".
`mail`: the cargo written in the mail alone, in the units and number formats customers use. The document family
also puts the other kinds' decisive fact in an attachment of each format, read by their handlers' own functions: a
customer's acceptance or decline (`booking.reply_intent`), the tracking desk's reference (`tracking.question`,
`match`), the airline's and our reference in a booking confirmation (`booking.confirmed_references`) and a
carrier's tariff (`tariffs.agent.handle`, priced against `tariffs/variants/variants.jsonl`).

`bid` (`--family bid`): a carrier's answer to our bid request, an airline's (total, currency, exchange rate, transit
days) or a trucker's (pickup price, currency, exchange rate, pickup hours), read by `pricing.bids.record` as a live
mail is: in the body, only in an attachment (pdf, docx, xlsx, txt, csv) or in both; as a sentence, key-value lines, a
table with the currency only in its header, a letter, a quotation, charges without a total, a rate per kg with or
without its total; beside a surcharge per hour, day, pallet or piece, an offer validity, an exchange rate line,
another lane's price or a date; in EUR (ISO code, euro sign, decimal comma, thousands by comma or space), USD, GBP
or CHF ("2'748.45"). A bid class the rules leave to the model costs a gateway call: it is named in the known gaps
too, and a named class that reads right fails the run like any stale gap.

Right: every judged field within 1% or 0.005 (a volume written with two decimals); neither moves a price out of
the 5% band. Model: the rules gave up and the model would read it, not measured offline. Wrong: the rules read a
field wrong, a lost round. A wrong case inside `tests/fixtures/rfqs/simulate-known-gaps.json` is a known gap; any
other fails the run.
"""

import argparse
import asyncio
import csv
import io
import itertools
import json
import random
import re
import sqlite3
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel import llm
from pixel.agents.rfq import booking, extract
from pixel.agents.tariffs import agent as tariffs
from pixel.agents.tracking import agent as tracking
from pixel.config import settings
from pixel.contracts import Attachment, Message, Rfq
from pixel.core import db, events
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import bids, session, store
from pixel.pricing.calc import price_tariff

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "rfqs"
VARIANTS = FIXTURES.parent / "tariffs" / "variants"
SOURCES = json.loads((FIXTURES / "simulate-sources.json").read_text())
KNOWN_GAPS = FIXTURES / "simulate-known-gaps.json"
RECEIVED = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
LB = D("0.45359237")
CUBIC_FOOT = D("0.028316846592")  # (0.3048 m)³
ORIGINS, DESTINATIONS = ("MUC", "FRA", "HAM", "STR", "DUS"), ("GRU", "SGN", "CAI", "ORD", "PVG", "YYZ")
GOODS = ("automotive sensors", "furniture samples", "spare parts", "laboratory glassware", "textile rolls")
TOLERANCE = D("0.01")
WRITTEN = D("0.005")  # half the last digit of a volume written with two decimals; under 1 kg chargeable


@dataclass
class Row:
    pieces: int
    sides_cm: tuple[int, int, int]  # length, width, height of one piece
    kg: int  # the row's gross weight, all its pieces

    @property
    def cbm(self) -> D:
        length, width, height = self.sides_cm
        return D(length * width * height * self.pieces) / D(1_000_000)


@dataclass
class Case:
    family: str
    axes: dict[str, str]
    subject: str
    body: str
    files: list[tuple[str, bytes]]
    want: dict[str, object]
    kind: str = "customer_rfq"
    request: dict[str, object] = field(default_factory=dict)  # a bid: the request it answers

    @property
    def disagrees(self) -> bool:
        """Mail and attachment state different figures."""
        return self.axes.get("relation") not in (None, "agrees", "silent")


@dataclass
class Outcome:
    case: Case
    verdict: str  # right, model, wrong
    wrong: list[str] = field(default_factory=list)
    gap: str | None = None


def _rows(rng: random.Random, count: int) -> list[Row]:
    rows = []
    for _ in range(count):
        n = rng.randint(1, 12)
        sides = (rng.choice((60, 80, 100, 120, 140)), rng.choice((40, 60, 80, 100, 120)), rng.randint(5, 17) * 10)
        rows.append(Row(n, sides, n * rng.randint(20, 300)))
    return rows


def _num(value: D | int, style: str, places: int = 0) -> str:
    """A number as written: plain, with a comma between thousands, or German."""
    text = f"{D(value):,.{places}f}"
    if style == "plain":
        return text.replace(",", "")
    if style == "de":
        return text.replace(",", "_").replace(".", ",").replace("_", ".")
    return text


LABELS = {
    "en": {
        "pieces": "Total pieces:", "weight": "Total gross weight (kg):", "volume": "Total volume (cbm):",
        "dg": "Dangerous goods:", "dest": "Destination airport:", "goods": "Goods description:",
        "L": "Length", "W": "Width", "H": "Height", "n": "Pieces", "kg": "Gross weight (kg)", "no": "Not restricted",
    },
    "de": {
        "pieces": "Packstücke gesamt:", "weight": "Bruttogewicht gesamt (kg):", "volume": "Volumen gesamt (cbm):",
        "dg": "Gefahrgut:", "dest": "Zielflughafen:", "goods": "Warenbeschreibung:",
        "L": "Länge", "W": "Breite", "H": "Höhe", "n": "Stück", "kg": "Bruttogewicht (kg)", "no": "Nein",
    },
}  # fmt: skip
TITLES = ("Packing List", "Packing Specification", "Shipper's Letter of Instruction")
PER_CM = {"cm": 1, "mm": 10}


def _document(rows: list[Row], cbm: D, dest: str, goods: str, axes: dict, rng: random.Random) -> list[list[str]]:
    """The document as lines of cells: heading, totals (stating `cbm`) and table, in the order `axes` names."""
    lab, style, unit = LABELS[axes["labels"]], axes["numbers"], axes["unit"]
    head = [[TITLES[rng.randrange(3)]], [f"No. {rng.randint(10000, 99999)}"], ["Date:", "2026-11-27"],
            ["Shipper:", "Nordlicht Audio GmbH, Kiel, Germany"], [lab["dest"], dest], [lab["goods"], goods]]  # fmt: skip
    totals = [[lab["pieces"], str(sum(r.pieces for r in rows))], [lab["weight"], _num(sum(r.kg for r in rows), style)],
              [lab["volume"], _num(cbm, style, 2)], [lab["dg"], lab["no"]]]  # fmt: skip
    columns = ["L", "W", "H", "n", "kg"]
    rng.shuffle(columns)
    side = {"L": 0, "W": 1, "H": 2}
    header = [lab[c] if c in ("n", "kg") else f"{lab[c]} ({unit})" for c in columns]
    body = [
        [str(r.pieces) if c == "n" else _num(r.kg, style) if c == "kg" else str(r.sides_cm[side[c]] * PER_CM[unit])
         for c in columns]
        for r in rows
    ]  # fmt: skip
    return {
        "before": head + totals + [header, *body],
        "after": head + [header, *body] + totals,
        "none": head + [totals[-1], header, *body],
        "only": head + totals,
    }[axes["totals"]]


def _pdf(lines: list[list[str]]) -> bytes:
    """A one-page PDF laid out as the scorer's: each cell its own text object, a label at x 50 and its value at 200."""
    ops, y = [], 790
    for cells in lines:
        step = min(100, 500 // len(cells))  # a tariff's nine columns still fit the page
        xs = (50, 200) if len(cells) == 2 else range(50, 50 + step * len(cells), step)
        for x, text in zip(xs, cells, strict=False):
            text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET")
        y -= 16
    stream = "\n".join(ops).encode("cp1252")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % o for o in offsets))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def _xlsx(lines: list[list[str]]) -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    for cells in lines:
        book.active.append([int(c) if c.isdigit() else c for c in cells])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _docx(lines: list[list[str]]) -> bytes:
    import docx

    document, table = docx.Document(), None
    width = max(len(c) for c in lines)
    for cells in lines:
        if len(cells) == width and width > 2:
            table = table or document.add_table(rows=0, cols=width)
            for cell, text in zip(table.add_row().cells, cells, strict=True):
                cell.text = text
        else:
            document.add_paragraph("  ".join(cells))
            table = None
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _txt(lines: list[list[str]]) -> bytes:
    return "\n".join("  ".join(c.ljust(20) for c in cells).rstrip() for cells in lines).encode()


def _csv(lines: list[list[str]]) -> bytes:
    """As a spreadsheet exports it: German decimal commas come with semicolons between the cells."""
    german = any(re.fullmatch(r"\d[\d.]*,\d+", c) for cells in lines for c in cells)
    out = io.StringIO()
    csv.writer(out, delimiter=";" if german else ",").writerows(lines)
    return out.getvalue().encode()


WRITERS = {"pdf": _pdf, "xlsx": _xlsx, "docx": _docx, "txt": _txt, "csv": _csv}
NAMES = {"pdf": ("SLI_{n}.pdf", "packing-spec_{n}.pdf"), "xlsx": ("PL_{n}.xlsx", "shipment_details_{n}.xlsx"),
         "docx": ("packing-list_{n}.docx",), "txt": ("packing-list_{n}.txt",), "csv": ("PL_{n}.csv",)}  # fmt: skip


def _cargo_sentence(pieces: int, kg: D, cbm: D | None, sides: tuple[int, int, int] | None, lang: str) -> str:
    dims = f"{sides[0]} x {sides[1]} x {sides[2]} cm" if sides else None
    if lang == "de":
        text = f"{pieces} Packstücke, Bruttogewicht {_num(kg, 'de')} kg"
        text += f", Volumen {_num(cbm, 'de', 2)} cbm" if cbm is not None else ""
        return text + (f". Maße je Stück {dims}." if dims else ".")
    text = f"It's {pieces} pieces, with a gross weight of {_num(kg, 'plain')} kg"
    text += f" and a total volume of {_num(cbm, 'plain', 2)} cbm" if cbm is not None else ""
    return text + (f". Each piece measures {dims}." if dims else ".")


def _mail(origin: str, dest: str, goods: str, cargo: str, lang: str, attached: bool) -> str:
    if lang == "de":
        return (f"Guten Tag,\n\nbitte senden Sie uns Ihr günstigstes Angebot für Luftfracht von {origin} nach {dest}. "
                f"Wir liefern die Sendung selbst an {origin} an, bereit ab 2026-11-27.\n\nEs handelt sich um {goods}: "
                f"{cargo}{' Die Packliste finden Sie im Anhang.' if attached else ''}\n\nViele Grüße\nMira Petersen")  # fmt: skip
    return (f"Hi,\n\nCould you quote an all-in price in EUR for the cheapest air freight option from {origin} to "
            f"{dest}? We'll deliver it to {origin} ourselves, and it'll be ready on 2026-11-27.\n\n{goods.capitalize()}. "
            f"{cargo}{' The packing list is attached.' if attached else ''}\n\nMira Petersen\nNordlicht Audio")  # fmt: skip


CORRECTIONS = {
    "correction_revised": ("en", "The corrected gross weight is {kg} kg."),
    "correction_use_mail": ("en", "Please use the gross weight in this email, not the attached packing list."),
    "correction_outdated": ("en", "Correction: the gross weight is now {kg} kg, the attached list is outdated."),
    "correction_de": ("de", "Korrektur: Das Bruttogewicht beträgt jetzt {kg} kg, die angehängte Liste ist veraltet."),
}
DOCUMENT_AXES = {
    "format": tuple(WRITERS),
    "totals": ("before", "after", "none", "only"),
    "rows": ("1", "2"),
    "unit": ("cm", "mm"),
    "labels": ("en", "de"),
    "numbers": ("plain", "thousands", "de"),
    "relation": tuple(SOURCES),
}


def _document_case(i: int, axes: dict[str, str], seed: int) -> Case:
    rng = random.Random(seed * 100_000 + i)
    relation = axes["relation"]
    rows = _rows(rng, int(axes["rows"]))
    origin, goods, dest = rng.choice(ORIGINS), rng.choice(GOODS), rng.choice(DESTINATIONS)
    doc_dest = rng.choice([d for d in DESTINATIONS if d != dest]) if relation == "other_shipment" else dest
    pieces, doc_kg = sum(r.pieces for r in rows), D(sum(r.kg for r in rows))
    measured = sum(r.cbm for r in rows)
    doc_cbm = measured.quantize(D("0.01")) if axes["totals"] != "none" else measured
    stated = (measured * D("1.6")).quantize(D("0.01")) if relation == "inconsistent" else doc_cbm
    sides = rows[0].sides_cm if len(rows) == 1 else None
    kg, cbm, n, mail_sides = doc_kg, doc_cbm, pieces, sides
    if relation in ("doc_weight", "doc_all", "other_shipment", *CORRECTIONS):
        kg = (doc_kg * D(rng.choice(("1.4", "1.5", "0.6")))).quantize(D(1))
    if relation in ("doc_volume", "doc_all", "other_shipment", "inconsistent"):
        mail_sides = (sides[0], sides[1], sides[2] + 60) if sides else None
        cbm = (doc_cbm * D("1.3")).quantize(D("0.01"))
    if relation == "pieces":
        n = pieces + 2
    lang = CORRECTIONS[relation][0] if relation in CORRECTIONS else rng.choice(("en", "de"))
    if relation == "silent":
        cargo = ("Pieces, weight and dimensions are in the attached list." if lang == "en"
                 else "Stückzahl, Gewicht und Maße stehen in der angehängten Liste.")  # fmt: skip
    else:
        cargo = _cargo_sentence(n, kg, cbm, mail_sides, lang)
    if relation in CORRECTIONS:
        cargo += " " + CORRECTIONS[relation][1].format(kg=kg)
    name = rng.choice(NAMES[axes["format"]]).format(n=rng.randint(10000, 99999))
    content = WRITERS[axes["format"]](_document(rows, stated, doc_dest, goods, axes, rng))
    figures = {"mail": {"weight_kg": kg, "volume_cbm": cbm}, "document": {"weight_kg": doc_kg, "volume_cbm": doc_cbm}}
    source = SOURCES[relation]
    want = {"origin": origin, "destination": dest} | {k: figures[source[k]][k] for k in ("weight_kg", "volume_cbm")}
    body = _mail(origin, dest, goods, cargo, lang, attached=True)
    return Case("document", axes, f"Air freight quote: {origin} to {dest}", body, [(name, content)], want)


def document_cases(seed: int = 7, step: int = 1) -> list[Case]:
    """Every combination of `DOCUMENT_AXES`; with `step`, every step-th (7 is prime to every axis's size)."""
    cases = []
    for i, values in enumerate(itertools.product(*DOCUMENT_AXES.values())):
        axes = dict(zip(DOCUMENT_AXES, values, strict=True))
        if i % step or (axes["totals"] == "only" and axes["rows"] == "2"):
            continue  # without a table its rows are invisible: the same case as one row
        if axes["relation"] == "inconsistent" and axes["totals"] in ("none", "only"):
            continue  # no stated total or no table to contradict it: the same document as doc_volume
        cases.append(_document_case(i, axes, seed))
    return cases + other_kind_cases()


REPLIES = {
    ("accept", "en"): "We accept your offer. Please book the shipment.",
    ("accept", "de"): "Wir nehmen Ihr Angebot an. Bitte buchen Sie die Sendung.",
    ("decline", "en"): "Thank you, but we will not go ahead with your offer.",
    ("decline", "de"): "Leider ist uns Ihr Angebot zu teuer, wir lehnen ab.",
}
COVER = {"en": "Hello,\n\nplease find our answer in the attached letter.\n\nBest regards\nMira Petersen",
         "de": "Guten Tag,\n\nunsere Antwort finden Sie im angehängten Schreiben.\n\nViele Grüße\nMira Petersen"}  # fmt: skip
REFERENCES = {"airline": ("Booking reference:", "BK-7Q4XK2M9"), "team": ("Your reference:", "PX-A1B2C3")}


def _tariff_sheet() -> tuple[list[list[str]], dict[str, str]]:
    """The control sheet of the tariff variants as cells, and its lane facts and spot prices by its generator."""
    row = json.loads((VARIANTS / "variants.jsonl").read_text().splitlines()[0])
    text = (VARIANTS / row["file"]).read_text()
    cells = [[c.strip() for c in line.split("  ") if c.strip()] for line in text.splitlines()]
    want = {f"{s['lane']} {s['weight_kg']} kg": f"{s['total']} on {s['billed_kg']}" for s in row["spots"]}
    want |= {
        f"{lane} transit, minimum": f"{f['transit_days']}, {f['minimum']}" for lane, f in row["lane_facts"].items()
    }
    return [c for c in cells if c], want


def _spoken_file(lines: list[list[str]]) -> bytes:
    """A voice message as its transcript: `run` reads it back for `tariffs._transcribe`, the speech model offline."""
    return "\n".join(" ".join(cells) for cells in lines).encode()


SPOKEN_MONEY = {"EUR": "euros", "USD": "dollars", "GBP": "pounds", "CHF": "francs"}


def _said(text: str) -> str:
    """A price as a voice message's transcript says it: "EUR 1,234.56" is "1234 euros and 56 cents"; an exchange rate
    keeps its figures: "1 USD = 1.277 EUR" is "1 dollars = 1.277 euros"."""

    def name(code: str) -> str:
        return SPOKEN_MONEY.get(bids.SYMBOLS.get(code, code), code)

    def say(m: re.Match) -> str:
        units, cents = divmod(bids._amount(m.group("n1") or m.group("n2")), 1)
        return f"{int(units)} {name(m.group('c1') or m.group('c2'))}" + (
            f" and {int(cents * 100)} cents" if cents else ""
        )

    def line(text: str) -> str:
        if bids.FX.search(text):
            return re.sub(rf"\b({'|'.join(SPOKEN_MONEY)})\b", lambda m: name(m[1]), text)
        return bids.PRICED.sub(say, text)

    return "\n".join(line(t) for t in text.splitlines())


def other_kind_cases() -> list[Case]:
    """Each other kind's decisive fact only in the attachment, in every format the RFQ documents come in, and said in
    a voice message (rounds 65 and 67 brought requests and offers as mp3)."""
    cases, (sheet, prices) = [], _tariff_sheet()
    for fmt, write in {**WRITERS, "mp3": _spoken_file}.items():
        for (says, lang), text in REPLIES.items():
            lines = [["Re: your quotation"], ["Date:", "2026-11-21"], [text], ["Mira Petersen, Nordlicht Audio"]]
            cases.append(Case("document", {"kind": "customer_reply", "format": fmt, "says": says, "lang": lang},
                              "Re: Air freight quote", COVER[lang], [(f"answer.{fmt}", write(lines))],
                              {"intent": says}, "customer_reply"))  # fmt: skip
        for which, (label, reference) in REFERENCES.items():
            lines = [["Shipment enquiry"], ["Customer:", "Nordlicht Audio"], [label, reference]]
            cases.append(Case("document", {"kind": "tracking_question", "format": fmt, "reference": which},
                              "Status request", "Hello,\n\nwhere is the shipment in the attached enquiry?",
                              [(f"enquiry.{fmt}", write(lines))], {"reference": reference}, "tracking_question"))  # fmt: skip
        lines = [["Booking confirmation"], *(list(r) for r in REFERENCES.values()), ["Status:", "booked"]]
        cases.append(Case("document", {"kind": "booking_confirmation", "format": fmt}, "Booking confirmation",
                          "Hello,\n\nplease find the confirmation of your booking attached.\n\nKestrel Airways",
                          [(f"confirmation.{fmt}", write(lines))], {k: r for k, (_, r) in REFERENCES.items()},
                          "booking_confirmation"))  # fmt: skip
        if fmt == "mp3":  # a tariff is no voice message
            continue
        cases.append(Case("document", {"kind": "tariff", "format": fmt}, "Tariff update",
                          "Hello,\n\nplease find our new air export tariff attached.\n\nTestline Cargo",
                          [(f"testline-cargo-tariff-2026-11.{fmt}", write(sheet))], prices, "tariff"))  # fmt: skip
    return cases


MAIL_AXES = {
    "weight": (
        "kg",
        "thousands",
        "de",
        "label",
        "lbs",
        "tonnes",
        "per_piece",
        "each_after",
        "gross_each",
        "a_piece",
        "per_pallet",
        "je",
        "lb",
        "pounds",
        "kgs",
        "kilos",
        "kilograms",
    ),
    "volume": (
        "cbm",
        "cbm_each",
        "m3_de",
        "dims_cm",
        "dims_mm",
        "dims_in",
        "dims_line",
        "cubic_feet",
        "ft3",
        "cu_ft",
        "litres",
        "dims_m",
    ),
    "lang": ("en", "de"),
    "lots": ("1", "2"),
}


PER_PIECE_FORMS = {"per_piece": "each {} kg", "each_after": "{} kg each", "gross_each": "{} kg gross each",
                   "a_piece": "à {} kg", "per_pallet": "{} kg per pallet", "je": "je {} kg"}  # fmt: skip


def _weight_text(kg: D, pieces: int, form: str) -> tuple[str, D]:
    """The weight as written, and the kg it means."""
    if form in ("lbs", "lb", "pounds"):
        lbs = (kg / LB).quantize(D(1))
        return f"{lbs} {form}", (lbs * LB).quantize(D("0.01"))
    if form == "tonnes":
        t = (kg / 1000).quantize(D("0.001"))
        return f"{t} t", t * 1000
    if form in PER_PIECE_FORMS:
        each = (kg / pieces).quantize(D(1))
        return PER_PIECE_FORMS[form].format(each), each * pieces
    return {"kg": f"{kg} kg", "thousands": f"{kg:,} kg", "de": f"{_num(kg, 'de')} kg",
            "label": f"gross weight: {kg} kg", "kgs": f"{kg} kgs", "kilos": f"{kg} kilos",
            "kilograms": f"{kg} kilograms"}[form], kg  # fmt: skip


def _volume_text(row: Row, form: str) -> tuple[str, D]:
    length, width, height = row.sides_cm
    cbm = row.cbm.quantize(D("0.01"))
    if form == "cbm":
        return f"{cbm} cbm", cbm
    if form == "cbm_each":
        each = (row.cbm / row.pieces).quantize(D("0.01"))
        return f"{each} cbm each", each * row.pieces
    if form == "m3_de":
        return f"{_num(cbm, 'de', 2)} m³", cbm
    if form in ("cubic_feet", "ft3", "cu_ft"):
        feet = (row.cbm / CUBIC_FOOT).quantize(D("0.01"))
        unit = {"cubic_feet": "cubic feet", "ft3": "ft³", "cu_ft": "cu ft"}[form]
        return f"{feet} {unit}", feet * CUBIC_FOOT
    if form == "litres":
        litres = (row.cbm * 1000).quantize(D(1))
        return f"{litres} litres", litres / 1000
    if form == "dims_m":
        return " x ".join(f"{D(s) / 100:.2f}" for s in row.sides_cm) + " m each", row.cbm
    if form == "dims_in":
        inch = [(D(s) / D("2.54")).quantize(D(1)) for s in row.sides_cm]
        return f"{inch[0]} x {inch[1]} x {inch[2]} in each", inch[0] * inch[1] * inch[2] * D("0.0254") ** 3 * row.pieces
    if form == "dims_mm":
        return f"{length * 10} x {width * 10} x {height * 10} mm each", row.cbm
    return f"{length} x {width} x {height} cm each", row.cbm


def mail_cases(seed: int = 11) -> list[Case]:
    cases = []
    for i, values in enumerate(itertools.product(*MAIL_AXES.values())):
        axes = dict(zip(MAIL_AXES, values, strict=True))
        rng = random.Random(seed * 100_000 + i)
        rows = _rows(rng, int(axes["lots"]))
        origin, dest, goods = rng.choice(ORIGINS), rng.choice(DESTINATIONS), rng.choice(GOODS)
        parts, kg, cbm = [], D(0), D(0)
        for j, row in enumerate(rows, 1):
            weight, row_kg = _weight_text(D(row.kg), row.pieces, axes["weight"])
            volume, row_cbm = _volume_text(row, axes["volume"])
            line = f"{row.pieces} {'Paletten' if axes['lang'] == 'de' else 'pallets'}, {weight}, {volume}"
            parts.append(f"Lot {j}: {line}" if len(rows) > 1 else line[0].upper() + line[1:] + ".")
            kg, cbm = kg + row_kg, cbm + row_cbm
        cargo = "\n" + "\n".join(parts) if len(rows) > 1 else parts[0]
        cases.append(Case("mail", axes, f"Air freight quote: {origin} to {dest}",
                          _mail(origin, dest, goods, cargo, axes["lang"], attached=False), [],
                          {"origin": origin, "destination": dest, "weight_kg": kg, "volume_cbm": cbm}))  # fmt: skip
    return cases


CARRIERS = {"airline": ("Kestrel Air Cargo", "kestrel@carrier.example"),
            "trucker": ("Nordwind Trucking", "nordwind@carrier.example")}  # fmt: skip
BID_AXES = {
    "carrier": tuple(CARRIERS),
    "place": ("body", "pdf", "docx", "xlsx", "txt", "csv", "both", "mp3"),
    "layout": ("sentence", "unlabelled", "keyvalue", "table", "letter", "quotation", "components", "per_kg",
               "per_kg_total"),
    "distractor": ("none", "per_hour", "per_day", "per_pallet", "per_piece", "validity", "fx_line", "second_lane",
                   "date", "from_price", "minimum", "earlier_offer", "option", "upgrade"),
    "money": ("iso", "sign", "de", "thousands", "space", "usd", "gbp", "chf"),
}  # fmt: skip
NUMBERS = {"iso": "plain", "sign": "plain", "de": "de", "thousands": "thousands", "space": "space", "usd": "thousands",
           "gbp": "plain", "chf": "swiss"}  # fmt: skip
HEADER = {"iso": "EUR", "sign": "€", "de": "EUR", "thousands": "EUR", "space": "EUR", "usd": "USD", "gbp": "GBP",
          "chf": "CHF"}  # fmt: skip


def _written(amount: D, style: str) -> str:
    """An amount in a carrier's number style: also "1 234,50" and the Swiss "1'234.50"."""
    places = max(2, -amount.as_tuple().exponent)
    if style == "space":
        return _num(amount, "de", places).replace(".", " ")
    if style == "swiss":
        return _num(amount, "thousands", places).replace(",", "'")
    return _num(amount, style, places)


def _priced(amount: D, style: str, before: bool) -> str:
    """An amount with its currency as carriers write it; a rate keeps its own decimals."""
    number, code = _written(amount, NUMBERS[style]), HEADER[style]
    if code == "€":
        return f"€{number}" if before else f"{number} €"
    return f"{code} {number}" if before else f"{number} {code}"


def _offer(kind: str, layout: str, f: dict, money: str, rng: random.Random) -> tuple[list[list[str]], D]:
    """The offer as lines of cells and the total a right reader takes from it."""
    before = rng.random() < 0.5
    m = lambda x: _priced(x, money, before)
    n = lambda x: _written(x, NUMBERS[money])
    ck, total, rate = f["kg"], f["total"], f["rate"]
    per_kg = (rate * ck).quantize(D("0.01"))
    parts = f["parts"]  # (description, quantity, unit price, amount)
    if kind == "airline":
        when, when_kv = f"The transit time is {f['time']} days.", ["Transit time", f"{f['time']} days"]
        price = f"Our all-in rate from {f['o']} to {f['d']} is {m(total)}, including freight, fuel and security."
        letter = (f"Our price for this shipment is {m(total)}. This price is all-in. It consists of "
                  + ", ".join(f"{p[0].lower()} {m(p[3])}" for p in parts) + ".")  # fmt: skip
        title, route = "AIR FREIGHT QUOTATION", ["Route", f"{f['o']} to {f['d']}"]
    else:
        when, when_kv = f"Our driver can be at the pickup address {f['time']} hours after your order.", [
            "Lead time", f"{f['time']} hours from order to collection"]  # fmt: skip
        price = f"We can collect your shipment at postcode {f['pc']} and deliver it to {f['o']} airport for {m(total)}."
        letter = (f"Our price for this transport is {m(total)}. This price is all-in. It has a "
                  + " and a ".join(f"{p[0].lower()} of {m(p[3])}" for p in parts) + ".")  # fmt: skip
        title, route = "PICKUP QUOTATION", ["Route", f"{f['pc']} to {f['o']} airport"]
    ref = ["Offer no.", f"Q-{rng.randint(1000, 9999)}"]
    lines = {
        "sentence": [[price], [when]],
        "unlabelled": [[f"Hello, we can offer {m(total)} for this shipment."], [when]],
        "keyvalue": [ref, route, ["All-in price", m(total)], when_kv],
        "table": [ref, route, when_kv, ["Pos", "Description", "Quantity", f"Unit price ({HEADER[money]})",
                  f"Amount ({HEADER[money]})"], *[[str(i), p[0], p[1], n(p[2]), n(p[3])] for i, p in
                  enumerate(parts, 1)], ["Total", n(total)]],
        "letter": [["Dear customer,"], [f"thank you for your enquiry. We calculated with {ck} kg chargeable weight."],
                   [letter], [when], ["Kind regards"]],
        "quotation": [[title], ref, route, ["Chargeable weight", f"{ck} kg"], *[[p[0], m(p[3])] for p in parts],
                      ["Total, all-in", m(total)], when_kv],
        "components": [ref, route, *[[p[0], m(p[3])] for p in parts], when_kv],
        "per_kg": [[f"Our all-in rate is {m(rate)} per kg chargeable weight."], [when]],
        "per_kg_total": [[f"All-in rate {m(rate)}/kg x {ck} kg = {m(per_kg)}"], [when]],
    }[layout]  # fmt: skip
    return lines, per_kg if layout.startswith("per_kg") else total


def _bid_case(i: int, axes: dict[str, str], seed: int) -> Case:
    rng = random.Random(seed * 100_000 + i)
    kind, money = axes["carrier"], axes["money"]
    cents = lambda lo, hi: D(rng.randint(lo, hi)) / 100
    kg = rng.randint(80, 2400)
    if kind == "airline":
        unit = (cents(120, 650), cents(40, 140), cents(10, 30))
        names = ("Air freight", "Fuel surcharge", "Security surcharge")
        parts = [(name, f"{kg} kg", u, (u * kg).quantize(D("0.01"))) for name, u in zip(names, unit, strict=True)]
        time, rate = rng.randint(1, 7), sum(unit)
    else:
        base, unit = cents(4000, 13000), cents(4, 12)
        parts = [("Base charge", "1", base, base), ("Weight charge", f"{kg} kg", unit, (unit * kg).quantize(D("0.01")))]
        time, rate = rng.randint(3, 36), cents(12, 30)
    o, d, other = rng.choice(ORIGINS), *rng.sample(DESTINATIONS, 2)
    f = {"kg": kg, "parts": parts, "total": sum(p[3] for p in parts), "rate": rate, "time": time, "o": o, "d": d,
         "pc": f"{rng.randint(10, 99)}{rng.randint(100, 999)}"}  # fmt: skip
    lines, total = _offer(kind, axes["layout"], f, money, rng)
    currency = {"usd": "USD", "gbp": "GBP", "chf": "CHF"}.get(money, "EUR")
    fx = D(rng.randint(7000, 13000)) / 10_000 if currency != "EUR" else None
    if money in ("usd", "chf"):
        lines.append([f"Exchange rate: 1 {currency} = {fx} EUR"])
    elif money == "gbp":
        lines.append([f"Exchange rate: {fx} EUR per GBP"])
    unit = "days" if kind == "airline" else "hours"
    other_time = rng.choice([t for t in (2, 5, 14, 24, 48) if t != time])
    extra = _priced(cents(1500, 9500), money, rng.random() < 0.5)
    near = _priced((total * cents(70, 130)).quantize(D("0.01")), money, rng.random() < 0.5)  # another offer's price
    distractor = {
        "per_hour": f"Not part of this offer: waiting time after the first 30 minutes, {extra} for each hour.",
        "per_day": f"Storage at the airport after 2 days: {extra} per day.",
        "per_pallet": f"Pallet exchange on request: {extra} per pallet.",
        "per_piece": f"Labelling on request: {extra} per piece.",
        "validity": f"This offer is valid for {other_time} {unit}.",
        "fx_line": "For reference: 1 SEK = 0.0871 EUR.",
        "second_lane": f"Note: our rate to {other} last week was {near}, with a transit time of {other_time} days."
        if kind == "airline" else f"Note: a pickup at the other postcode would be {near}.",
        "date": "Ready at the warehouse on 27.11.2026 from 09:00, flight on 28 Nov 2026.",
        # Prices that are not this offer's: a "from" price, a minimum, an earlier offer, an option, an upgrade.
        "from_price": f"For smaller shipments our rates start from {extra}.",
        "minimum": f"Minimum charge {extra}.",
        "earlier_offer": f"Our previous offer on this route was {near}.",
        "option": f"Optional insurance: {extra}.",
        "upgrade": f"An upgrade to express service costs {extra} extra.",
    }.get(axes["distractor"])  # fmt: skip
    if distractor:
        lines.insert(rng.randint(0, len(lines)), [distractor])
    place, files = axes["place"], []
    fmt = rng.choice(tuple(WRITERS)) if place == "both" else place
    name = f"offer-Q{rng.randint(1000, 9999)}.{fmt}"
    text = "\n".join("  ".join(c) for c in lines)
    if place == "mp3":  # a voice message says the offer (round 65's truckers)
        name = "voice-message.mp3"
        files.append((name, _said(text).encode()))
    elif place != "body":  # a pdf wraps a long line as a letter does
        wrap = fmt == "pdf"
        doc = [w for c in lines for w in ([[t] for t in textwrap.wrap(c[0], 90)] if wrap and len(c) == 1 else [c])]
        files.append((name, WRITERS[fmt](doc)))
    attached = f"Please find our offer in the attached file {name}."
    offer = {"body": text, "both": f"{text}\n\nThe same offer is attached as {name}."}.get(place, attached)
    body = f"Hello,\n\nthank you for your request.\n\n{offer}\n\nBest regards\n{CARRIERS[kind][0]}"
    want = {"total": total, "currency": currency, "fx_eur": fx, "transit_days" if kind == "airline" else
            "pickup_hours": time}  # fmt: skip
    subject = f"Re: Rate request {o}-{d}" if kind == "airline" else f"Re: Pickup request {f['pc']} to {o}"
    return Case(
        "bid",
        axes,
        subject,
        body,
        files,
        want,
        kind=f"{'airline' if kind == 'airline' else 'pickup'}_bid",
        request={"origin": o, "destination": d, "weight_kg": D(kg)},
    )


def bid_cases(seed: int = 13, step: int = 1) -> list[Case]:
    """Every combination of `BID_AXES`; with `step`, every step-th."""
    grid = itertools.product(*BID_AXES.values())
    return [_bid_case(i, dict(zip(BID_AXES, v, strict=True)), seed) for i, v in enumerate(grid) if not i % step]


class Mailbox:
    def __init__(self, files: list[tuple[str, bytes]]):
        self.files = dict(enumerate(content for _, content in files))

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[attachment_id]


class ModelNeeded(Exception):
    pass


def _close(got, want) -> bool:
    if isinstance(want, D):  # within 1%, or within the mail's written precision (0.38 cbm for a table's 0.384)
        return abs(D(got) - want) <= max(want * TOLERANCE, WRITTEN)
    return got == want


def known_gaps() -> list[dict]:
    return json.loads(KNOWN_GAPS.read_text())


def gap_of(case: Case, gaps: list[dict]) -> str | None:
    """The known gap a case falls in: every axis the gap names has the case's value (a list: one of them)."""
    for gap in gaps:
        if gap["family"] == case.family and all(
            case.axes[k] in (v if isinstance(v, list) else [v]) for k, v in gap["axes"].items()
        ):
            return gap["note"]
    return None


async def _rfq(ctx: Context, message: Message, case: Case) -> dict:
    rfq, _ = await extract.read_request(ctx, message, deadline_for(RECEIVED, ctx.settings))
    return {k: getattr(rfq, k) for k in case.want}


async def _reply(ctx: Context, message: Message, case: Case) -> dict:
    return {"intent": await booking.reply_intent(ctx, message)}


async def _tracking(ctx: Context, message: Message, case: Case) -> dict:
    return {"reference": tracking.match(ctx, await tracking.question(ctx, message), message.sender)[1]}


async def _tariff(ctx: Context, message: Message, case: Case) -> dict:
    """Ingested into a store of its own, then priced at the generator's spot weights."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init(conn)
    ctx = Context(ctx.settings, conn, ctx.mailbox, None, None, None)
    await tariffs.handle(ctx, message)
    got = {}
    for key in case.want:
        lane, spot = key.split(" ", 1)
        origin, destination = lane.split("-")
        found = store.lookup(ctx, origin, destination, RECEIVED.date())
        if not found:
            got[key] = "not stored"
        elif spot.endswith(" kg"):
            rfq = Rfq(session_id=1, message_id=1, customer="simulate", origin=origin, destination=destination,
                      weight_kg=D(spot.split()[0]), received_at=RECEIVED, deadline_at=RECEIVED)  # fmt: skip
            option = price_tariff(found[0], rfq)
            got[key] = f"{option.total} on {option.billed_kg}"
        else:
            got[key] = f"{found[0].transit_days}, {found[0].minimum}"
    return got


async def _confirmation(ctx: Context, message: Message, case: Case) -> dict:
    return dict(zip(REFERENCES, await booking.confirmed_references(ctx, message), strict=True))


READERS = {"customer_rfq": _rfq, "customer_reply": _reply, "tracking_question": _tracking, "tariff": _tariff,
           "booking_confirmation": _confirmation}  # fmt: skip


async def read(case: Case, ctx: Context) -> Outcome:
    message = Message(
        id=1, session_id=1, direction="in", sender="buyer@example.com", to="us@example.com", subject=case.subject,
        body=case.body, sent_at=RECEIVED,
        attachments=[Attachment(id=i, filename=n, content_type="", size=len(c)) for i, (n, c) in enumerate(case.files)],
    )  # fmt: skip
    ctx.mailbox = Mailbox(case.files)
    called = []

    async def model(ctx, **kw):
        called.append(1)
        raise ModelNeeded

    llm.ask_json = model
    try:
        got = await READERS[case.kind](ctx, message, case)
    except ModelNeeded:
        return Outcome(case, "model")
    if called:
        return Outcome(case, "model")
    wrong = [f"{k}: {got[k]} != {v}" for k, v in case.want.items() if not _close(got[k], v)]
    return Outcome(case, "wrong" if wrong else "right", wrong)


async def read_bid(case: Case, ctx: Context, i: int) -> Outcome:
    """A carrier's answer through the path a live mail takes, `bids.record`, each in a session of its own."""
    email = CARRIERS[case.axes["carrier"]][1]
    rfq = Rfq(session_id=i, message_id=i, customer="buyer@example.com", received_at=RECEIVED, deadline_at=RECEIVED,
              **case.request)  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=i, payload=rfq.model_dump(mode="json"))
    message = Message(
        id=i, session_id=i, direction="in", sender=email, to="us@example.com", subject=case.subject, body=case.body,
        sent_at=RECEIVED,
        attachments=[Attachment(id=j, filename=n, content_type="", size=len(c)) for j, (n, c) in enumerate(case.files)],
    )  # fmt: skip
    ctx.mailbox = Mailbox(case.files)
    called = []

    async def model(ctx, **kw):
        called.append(1)
        raise ModelNeeded

    llm.ask_json = model
    await bids.record(ctx, message)
    found = session.options(ctx, i)
    if called and not found:
        return Outcome(case, "model")
    if len(found) != 1:
        return Outcome(case, "wrong", [f"{len(found)} options"])
    got = found[0].model_dump()
    wrong = [f"{k}: {got[k]} != {v}" for k, v in case.want.items() if got[k] != v]
    return Outcome(case, "wrong" if wrong else "right", wrong)


async def run(cases: list[Case]) -> list[Outcome]:
    """Each case on one in-memory database, with every model call refused and counted; mail stays dry."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init(conn)
    # The rules are measured: the model's cross-check of a request they read (`extract._crosscheck`) would count
    # every case as left to the model.
    ctx = Context(replace(settings(), model_check=""), conn, None, None, None, None)
    ask_json, transcribe, gaps = llm.ask_json, tariffs._transcribe, known_gaps()
    tariffs._transcribe = lambda content: content.decode().splitlines()  # `_spoken_file`: the transcript itself
    if any(c.family == "bid" for c in cases):
        store.save_carriers(ctx, [{"name": n, "kind": k, "email": e, "serves": ""} for k, (n, e) in CARRIERS.items()])
    try:
        outcomes = [await (read_bid(c, ctx, i) if c.family == "bid" else read(c, ctx)) for i, c in enumerate(cases, 1)]
    finally:
        llm.ask_json, tariffs._transcribe = ask_json, transcribe
    for o in outcomes:  # a bid the rules leave to the model costs spend: its class is named too
        if o.verdict == "wrong" or (o.verdict == "model" and o.case.family == "bid"):
            o.gap = gap_of(o.case, gaps)
    return outcomes


def stale_gaps(outcomes: list[Outcome]) -> list[str]:
    """Known gaps no case falls in any more: they read right now, so their note goes."""
    hit = {o.gap for o in outcomes if o.gap}
    return [g["note"] for g in known_gaps() if g["note"] not in hit]


def report(outcomes: list[Outcome]) -> None:
    groups = defaultdict(list)
    for o in outcomes:
        groups[o.case.family if o.case.kind == "customer_rfq" else f"{o.case.family}: {o.case.kind}"].append(o)
    for family, mine in groups.items():
        count = {v: sum(o.verdict == v for o in mine) for v in ("right", "model", "wrong")}
        known = sum(bool(o.gap) for o in mine if o.verdict == "wrong")
        print(f"\n[{family}] {len(mine)} cases: {count['right']} right, {count['model']} model, "
              f"{count['wrong']} wrong ({known} in known gaps)")  # fmt: skip
        for axis in mine[0].case.axes:
            tally = defaultdict(lambda: [0, 0, 0])
            for o in mine:
                tally[o.case.axes[axis]][("right", "model", "wrong").index(o.verdict)] += 1
            print(f"  {axis:8} " + "  ".join(f"{v}={r}/{m}/{w}" for v, (r, m, w) in tally.items()))
    print("\n  (each value: right/model/wrong)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", type=int, default=0, help="print this many wrong cases outside the known gaps")
    parser.add_argument("--family", choices=("document", "mail", "bid"))
    parser.add_argument("--seed", type=int, help="draw other figures for the same grid (default: 7, 11 and 13)")
    args = parser.parse_args()
    seeds = {} if args.seed is None else {"seed": args.seed}
    every = (*document_cases(**seeds), *mail_cases(**seeds), *bid_cases(**seeds))
    cases = [c for c in every if args.family in (None, c.family)]
    outcomes = asyncio.run(run(cases))
    report(outcomes)
    unknown = [o for o in outcomes if o.verdict == "wrong" and not o.gap]
    for o in unknown[: args.show]:
        print(f"\n--- {o.case.family} {o.case.axes}\n{'; '.join(o.wrong)}\n{o.case.body}")
    stale = stale_gaps(outcomes) if not (args.family or seeds) else []
    print(f"\n{len(unknown)} wrong outside the known gaps")
    if stale:
        print("known gaps that read right now, delete their note:\n  " + "\n  ".join(stale))
    return 1 if unknown or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
