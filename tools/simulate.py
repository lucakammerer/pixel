"""Generated requests, read offline by `extract.read_request`: a whole class of wording or document read wrong shows
here before a round is lost to it. Every case is built from known figures and its answer is computed here, never
by the code under test.

uv run python -m tools.simulate            # per axis: read right, left to the model, read wrong
uv run python -m tools.simulate --show 10  # the first wrong cases outside the known gaps, in full

Two families. `document`: a mail with an attached packing list, letter of instruction or specification (PDF, xlsx,
docx) in the layouts seen since round 33 and their neighbours. Which source a right reading takes depends on how
mail and document relate (`relation`): `tests/fixtures/rfqs/simulate-sources.json` says it per relation and why, so
a correction the customer makes or an attachment for another shipment is never read as "the document wins".
`mail`: the cargo written in the mail alone, in the units and number formats customers use.

Right: every judged field within 1% (a 1% volume error moves no price out of the 5% band). Model: the rules gave
up and the model would read it, not measured offline. Wrong: the rules read a field wrong, a lost round. A wrong
case inside `tests/fixtures/rfqs/simulate-known-gaps.json` is a known gap; any other fails the run.
"""

import argparse
import asyncio
import io
import itertools
import json
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel import llm
from pixel.agents.rfq import extract
from pixel.config import settings
from pixel.contracts import Attachment, Message
from pixel.core import db
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "rfqs"
SOURCES = json.loads((FIXTURES / "simulate-sources.json").read_text())
KNOWN_GAPS = FIXTURES / "simulate-known-gaps.json"
RECEIVED = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
LB = D("0.45359237")
ORIGINS, DESTINATIONS = ("MUC", "FRA", "HAM", "STR", "DUS"), ("GRU", "SGN", "CAI", "ORD", "PVG", "YYZ")
GOODS = ("automotive sensors", "furniture samples", "spare parts", "laboratory glassware", "textile rolls")
TOLERANCE = D("0.01")


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
        xs = (50, 200) if len(cells) == 2 else range(50, 50 + 100 * len(cells), 100)
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


WRITERS = {"pdf": _pdf, "xlsx": _xlsx, "docx": _docx}
NAMES = {"pdf": ("SLI_{n}.pdf", "packing-spec_{n}.pdf"), "xlsx": ("PL_{n}.xlsx", "shipment_details_{n}.xlsx"),
         "docx": ("packing-list_{n}.docx",)}  # fmt: skip


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
    "format": ("pdf", "xlsx", "docx"),
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
        cases.append(_document_case(i, axes, seed))
    return cases


MAIL_AXES = {
    "weight": ("kg", "thousands", "de", "label", "lbs", "tonnes", "per_piece"),
    "volume": ("cbm", "m3_de", "dims_cm", "dims_mm", "dims_in", "dims_line"),
    "lang": ("en", "de"),
    "lots": ("1", "2"),
}


def _weight_text(kg: D, pieces: int, form: str) -> tuple[str, D]:
    """The weight as written, and the kg it means."""
    if form == "lbs":
        lbs = (kg / LB).quantize(D(1))
        return f"{lbs} lbs", lbs * LB
    if form == "tonnes":
        t = (kg / 1000).quantize(D("0.001"))
        return f"{t} t", t * 1000
    if form == "per_piece":
        each = (kg / pieces).quantize(D(1))
        return f"each {each} kg", each * pieces
    return {"kg": f"{kg} kg", "thousands": f"{kg:,} kg", "de": f"{_num(kg, 'de')} kg",
            "label": f"gross weight: {kg} kg"}[form], kg  # fmt: skip


def _volume_text(row: Row, form: str) -> tuple[str, D]:
    length, width, height = row.sides_cm
    cbm = row.cbm.quantize(D("0.01"))
    if form == "cbm":
        return f"{cbm} cbm", cbm
    if form == "m3_de":
        return f"{_num(cbm, 'de', 2)} m³", cbm
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


class Mailbox:
    def __init__(self, files: list[tuple[str, bytes]]):
        self.files = dict(enumerate(content for _, content in files))

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[attachment_id]


class ModelNeeded(Exception):
    pass


def _close(got, want) -> bool:
    if isinstance(want, D):
        return abs(D(got) - want) <= want * TOLERANCE
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
        rfq, _ = await extract.read_request(ctx, message, deadline_for(RECEIVED, ctx.settings))
    except ModelNeeded:
        return Outcome(case, "model")
    if called:
        return Outcome(case, "model")
    wrong = [f"{k}: {getattr(rfq, k)} != {v}" for k, v in case.want.items() if not _close(getattr(rfq, k), v)]
    return Outcome(case, "wrong" if wrong else "right", wrong)


async def run(cases: list[Case]) -> list[Outcome]:
    """Each case on one in-memory database, with every model call refused and counted; mail stays dry."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init(conn)
    ctx = Context(settings(), conn, None, None, None, None)
    ask_json, gaps = llm.ask_json, known_gaps()
    try:
        outcomes = [await read(case, ctx) for case in cases]
    finally:
        llm.ask_json = ask_json
    for o in outcomes:
        if o.verdict == "wrong":
            o.gap = gap_of(o.case, gaps)
    return outcomes


def stale_gaps(outcomes: list[Outcome]) -> list[str]:
    """Known gaps no case falls in any more: they read right now, so their note goes."""
    hit = {o.gap for o in outcomes if o.gap}
    return [g["note"] for g in known_gaps() if g["note"] not in hit]


def report(outcomes: list[Outcome]) -> None:
    for family in dict.fromkeys(o.case.family for o in outcomes):
        mine = [o for o in outcomes if o.case.family == family]
        count = {v: sum(o.verdict == v for o in mine) for v in ("right", "model", "wrong")}
        known = sum(bool(o.gap) for o in mine)
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
    parser.add_argument("--family", choices=("document", "mail"))
    args = parser.parse_args()
    cases = [c for c in (*document_cases(), *mail_cases()) if args.family in (None, c.family)]
    outcomes = asyncio.run(run(cases))
    report(outcomes)
    unknown = [o for o in outcomes if o.verdict == "wrong" and not o.gap]
    for o in unknown[: args.show]:
        print(f"\n--- {o.case.family} {o.case.axes}\n{'; '.join(o.wrong)}\n{o.case.body}")
    stale = stale_gaps(outcomes) if not args.family else []
    print(f"\n{len(unknown)} wrong outside the known gaps")
    if stale:
        print("known gaps that read right now, delete their note:\n  " + "\n  ".join(stale))
    return 1 if unknown or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
