"""Mail against its attached document: every figure the document states, changed in the mail, in every layout.

Rounds 33 to 35 were one scenario: the attached packing specification carries the shipment the scorer prices, and
the mail states one figure differently, its dimensions changed to match so the mail still agrees with itself
(2.78 instead of 1.92 cbm, 11.69 instead of 7.49 cbm, 2644 instead of 1751 kg). Each round brought another field
and another column order. This module turns the captured requests (`rfqs/document-conflicts.json`) into every such
case known in advance:

    base request x wrong field in the mail x column order x file format (pdf, xlsx, docx) x document layout

and reads each with the real `extract.extract`, the attachment served from memory. A right reading is the
document's figure. A request the rules leave to the model is counted apart: offline there is no model, and the
model sees both figures. `benchmark_generalization` section 11 prints and judges the results.
"""

import asyncio
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal as D
from itertools import product
from pathlib import Path
from unittest.mock import patch

from pixel import llm
from pixel.agents.rfq import extract
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LlmResult
from pixel.core import db
from pixel.core.context import Context

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "rfqs" / "document-conflicts.json"
JUDGED = ("origin", "destination", "weight_kg", "volume_cbm", "pieces")
HEADERS = {"weight": "Gross weight (kg)", "length": "Length (cm)", "width": "Width (cm)", "height": "Height (cm)",
           "pieces": "Pieces"}  # fmt: skip
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def volume(shipment: dict) -> D:
    cm3 = D(shipment["length"]) * D(shipment["width"]) * D(shipment["height"]) * shipment["pieces"]
    return (cm3 / D(1_000_000)).quantize(D("0.01"))


def _mail_figures(truth: dict, wrong: dict, change: str) -> dict:
    """The figures the mail states when it gets `change` wrong; everything else as in the document."""
    mail = dict(truth)
    if change in ("weight", "weight_and_volume"):
        mail["weight_kg"] = str((D(truth["weight_kg"]) * 3 / 2).quantize(D(1)))
    if change in ("volume", "weight_and_volume"):
        mail["height"] = truth["height"] + 40  # dimensions changed with the volume, as seen live
    if change == "pieces":
        mail["pieces"] = truth["pieces"] + 2
    if change == "destination":
        mail["destination"], mail["destination_name"] = wrong["destination"], wrong["destination_name"]
    mail["volume_cbm"] = str(volume(mail))
    if change == "volume_only":
        mail["volume_cbm"] = str(volume(truth) + D("1.30"))  # the mail's own dimensions no longer bear it out
    return mail


CHANGES = ("none", "weight", "volume", "volume_only", "pieces", "destination", "weight_and_volume")


LAYOUTS = ("live", "two_rows", "plain_labels", "millimetres", "weight_per_piece", "table_only")


def _thousands(kg: str) -> str:
    return f"{int(D(kg)):,}" if D(kg) == D(kg).to_integral_value() else kg


def _document_lines(base: dict, order: list[str], layout: str = "live"):
    """The document's labelled block, its table header and its rows, columns in `order`.

    `live` is the layout of rounds 33 to 35. The others are what the next document may look like: the pieces split
    over two rows (`two_rows`), labels with the unit after the figure and thousands separators (`plain_labels`),
    dimensions in millimetres (`millimetres`), the table's weight per piece (`weight_per_piece`), no total lines,
    only the table (`table_only`). Every layout states the same shipment."""
    doc, truth = base["document"], base["truth"]
    total_volume = str(volume(truth))
    if layout == "plain_labels":
        block = [
            ("Date:", doc["date"]),
            ("Shipper:", doc["shipper"]),
            ("Consignee:", doc["consignee"]),
            ("Destination:", truth["destination"]),
            ("Commodity:", doc["goods"]),
            ("Number of packages:", str(truth["pieces"])),
            ("Gross weight:", f"{_thousands(truth['weight_kg'])} kg"),
            ("Volume:", f"{total_volume} cbm"),
            ("DG:", "no"),
        ]
    else:
        block = [
            ("Date:", doc["date"]),
            ("Shipper:", doc["shipper"]),
            ("Consignee:", doc["consignee"]),
            ("Destination airport:", truth["destination"]),
            ("Goods description:", doc["goods"]),
            ("Total pieces:", str(truth["pieces"])),
            ("Total gross weight (kg):", truth["weight_kg"]),
            ("Total volume (cbm):", total_volume),
            ("Dangerous goods:", "Not restricted"),
        ]
    if layout == "table_only":
        block = [line for line in block if not line[0].startswith("Total")]
    scale = 10 if layout == "millimetres" else 1
    headers = {k: (v.replace("(cm)", "(mm)") if scale == 10 else v) for k, v in HEADERS.items()}
    if layout == "weight_per_piece":
        headers["weight"] = "Weight per piece (kg)"
    sides = {k: str(truth[k] * scale) for k in ("length", "width", "height")}
    if layout == "two_rows" and truth["pieces"] > 1:
        first = truth["pieces"] // 2
        kg = D(truth["weight_kg"])
        kg_first = (kg * first / truth["pieces"]).quantize(D(1))
        rows = [{"pieces": str(first), "weight": str(kg_first), **sides},
                {"pieces": str(truth["pieces"] - first), "weight": str(kg - kg_first), **sides}]  # fmt: skip
    elif layout == "weight_per_piece":
        each = (D(truth["weight_kg"]) / truth["pieces"]).quantize(D("0.01"))
        rows = [{"pieces": str(truth["pieces"]), "weight": f"{each.normalize():f}", **sides}]
    else:
        rows = [{"pieces": str(truth["pieces"]), "weight": truth["weight_kg"], **sides}]
    return block, [headers[c] for c in order], [[r[c] for c in order] for r in rows]


def _pdf(title: str, number: str, block, header, rows) -> bytes:
    """A one-page text pdf laid out as the live ones: labels at x 50, values at x 200, table columns 100 apart."""

    def text(font: str, size: int, x: int, y: int, s: str) -> str:
        s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"BT /{font} {size} Tf {x} {y} Td ({s}) Tj ET"

    ops, y = [text("F2", 16, 50, 790, title), text("F1", 10, 50, 766, number)], 740
    for label, value in block:
        ops += [text("F1", 10, 50, y, label), text("F1", 10, 200, y, value)]
        y -= 16
    y -= 10
    ops += [text("F2", 10, 50 + 100 * i, y, h) for i, h in enumerate(header)]
    for n, row in enumerate(rows, 1):
        ops += [text("F1", 10, 50 + 100 * i, y - 16 * n, v) for i, v in enumerate(row)]
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _xlsx(title: str, number: str, block, header, rows) -> bytes:
    """A sheet laid out as the live one: title, number, the block in A/B, a blank row, the table; numbers as numbers."""
    import openpyxl

    def cell(v: str):
        try:
            n = D(v)
        except ArithmeticError:
            return v
        return int(n) if n == n.to_integral_value() else float(n)

    book = openpyxl.Workbook()
    ws = book.active
    ws.append([title])
    ws.append([number])
    ws.append([])
    for label, value in block:
        ws.append([label, cell(value) if label.startswith("Total") else value])  # "1,751 kg" stays text
    ws.append([])
    ws.append(header)
    for row in rows:
        ws.append([cell(v) for v in row])
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _docx(title: str, number: str, block, header, rows) -> bytes:
    """The same document as Word: paragraphs for the block, a real table for the pieces."""
    import docx

    document = docx.Document()
    document.add_heading(title, level=1)
    document.add_paragraph(number)
    for label, value in block:
        document.add_paragraph(f"{label} {value}")
    table = document.add_table(rows=1 + len(rows), cols=len(header))
    for r, cells in enumerate([header, *rows]):
        for i, v in enumerate(cells):
            table.cell(r, i).text = v
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


RENDER = {"pdf": _pdf, "xlsx": _xlsx, "docx": _docx}


@dataclass
class Case:
    base: str  # the captured request it is built on
    change: str  # the field the mail states wrong
    order: int  # index into column_orders
    format: str
    layout: str
    raw: dict  # the mail as the mailbox returns it, attachment included
    want: dict  # the document's figures
    mail: dict  # the figures the mail states


def cases(fixture: Path = FIXTURE) -> list[Case]:
    import base64

    spec = json.loads(fixture.read_text())
    found = []
    axes = product(spec["bases"], CHANGES, enumerate(spec["column_orders"]), spec["formats"], LAYOUTS)
    for base, change, (o, order), fmt, layout in axes:
        truth, doc = base["truth"], base["document"]
        figures = _mail_figures(truth, base["wrong"], change)
        content = RENDER[fmt](
            doc["title"], f"No. {doc['name'].rsplit('_', 1)[-1]}", *_document_lines(base, order, layout)
        )
        raw = {
            "id": 1,
            "from": "customer@example.com",
            "to": "pixel@example.com",
            "subject": base["subject"].format(**figures),
            "body": base["body"].format(**figures),
            "in_reply_to": None,
            "references": None,
            "received_at": "2026-11-21T16:00:00+00:00",
            "attachments": [
                {
                    "id": 1,
                    "filename": f"{doc['name']}.{fmt}",
                    "content_type": CONTENT_TYPES[fmt],
                    "size": len(content),
                    "content_base64": base64.b64encode(content).decode(),
                }
            ],
        }
        want = {"origin": truth["origin"], "destination": truth["destination"], "weight_kg": D(truth["weight_kg"]),
                "volume_cbm": volume(truth), "pieces": truth["pieces"]}  # fmt: skip
        found.append(Case(base["seen_live"], change, o, fmt, layout, raw, want, figures))
    return found


class _Mailbox:
    def __init__(self, raw: dict):
        import base64

        self.files = {a["id"]: base64.b64decode(a["content_base64"]) for a in raw["attachments"]}

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[attachment_id]


class _ModelNeeded(Exception):
    pass


@dataclass
class Result:
    case: Case
    by_rules: bool
    wrong_fields: list[str] = field(default_factory=list)
    known_gap: str | None = None

    @property
    def right(self) -> bool | None:
        return None if not self.by_rules else not self.wrong_fields

    @property
    def name(self) -> str:
        c = self.case
        return f"{c.base} {c.change} {c.layout} order{c.order} {c.format}"


def gap_for(gaps: dict[str, str], case: Case) -> str | None:
    """A known gap is keyed by the changed field, or narrower by `field:layout`."""
    return gaps.get(f"{case.change}:{case.layout}") or gaps.get(case.change)


def _mail_reading(case: Case) -> LlmResult:
    """A cross-check that reads the mail's figures, as gpt-4.1 did in round 130 (1510 kg for the letter's 1144)."""
    m = case.mail
    data = {k: None for k in extract.SCHEMA["required"]} | {
        "origin": m["origin"], "destination": m["destination"], "weight_kg": m["weight_kg"],
        "volume_cbm": m["volume_cbm"], "pieces": m["pieces"], "lots": [], "preference": "cheapest",
        "door_pickup": False, "language": "en",
    }  # fmt: skip
    return LlmResult(data=data, model="adversarial", tokens_in=1, tokens_out=1, cost_usd=D(0))


async def _read(case: Case, adversarial: bool = False) -> Result:
    conn = db.connect(":memory:")
    db.init(conn)
    ctx = Context(settings(), conn, None, None, None, None)
    ctx.mailbox = _Mailbox(case.raw)
    asked = []

    async def no_model(*a, **kw):
        asked.append(kw.get("purpose"))
        if adversarial and kw.get("purpose") == "rfq.crosscheck":
            return _mail_reading(case)
        raise _ModelNeeded

    try:
        with patch.object(llm, "ask_json", no_model):
            rfq = await extract.extract(ctx, to_message(case.raw, "in"), extract_deadline())
    except _ModelNeeded:
        return Result(case, by_rules=False)
    finally:
        conn.close()
    if "rfq.extract" in asked:  # the rules passed: offline the guess path ran, live the model reads both figures
        return Result(case, by_rules=False)
    wrong = [f"{k}: {getattr(rfq, k)} != {v}" for k, v in case.want.items() if getattr(rfq, k) != v]
    return Result(case, True, wrong)


def extract_deadline():
    from datetime import UTC, datetime

    from pixel.core.orchestrator import deadline_for

    return deadline_for(datetime(2026, 11, 21, 16, 0, tzinfo=UTC), settings())


def audit(fixture: Path = FIXTURE, adversarial: bool = False) -> list[Result]:
    """With `adversarial`, the model's cross-check reads every figure as the mail states it."""
    gaps = json.loads(fixture.read_text()).get("known_gaps", {})

    async def run() -> list[Result]:
        return [await _read(c, adversarial) for c in cases(fixture)]

    results = asyncio.run(run())
    for r in results:
        r.known_gap = gap_for(gaps, r.case)
    return results


def by_change(results: list[Result], axis: str = "layout") -> dict[str, dict[str, tuple[int, int, int]]]:
    """Per changed field and document layout (or `axis="format"`): (right, wrong, left to the model)."""
    table: dict[str, dict[str, list[int]]] = {}
    for r in results:
        cell = table.setdefault(r.case.change, {}).setdefault(getattr(r.case, axis), [0, 0, 0])
        cell[0 if r.right else 1 if r.right is False else 2] += 1
    return {c: {f: tuple(v) for f, v in row.items()} for c, row in table.items()}
