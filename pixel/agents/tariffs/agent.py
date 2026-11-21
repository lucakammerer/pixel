import asyncio
import csv
import hashlib
import io
import itertools
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from pixel import llm
from pixel.api.client import ApiError
from pixel.contracts import LaneTariff, Message
from pixel.core import events
from pixel.core.context import Context
from pixel.pricing import store

NUM = r"\d+(?:[.,]\d+)?"
CODE = re.compile(r"\b[A-Z]{3}\b")
BREAK = r"(?:from\s+(\d+)\s*kg|(\d+)\s*kg\s*\+|([-+])(\d+))"
BREAK_TOKEN = re.compile(rf"(?:(?<=\s)|^){BREAK}(?=\s|$)", re.IGNORECASE)
BREAK_LINE = re.compile(rf"^\s*{BREAK}\s+({NUM})\s*$", re.IGNORECASE)
# The columns before the weight breaks, by header: "Minimum  Transit" as often as "Transit  Minimum".
LEAD_COLUMN = re.compile(
    r"\b(?:(?P<transit>transit|laufzeit|t/t)|(?P<minimum>minimum|min\.|mindest\w*))(?!\w)", re.IGNORECASE
)
IMAGES = {".jpg", ".jpeg", ".png"}
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y")


@dataclass
class Sheet:
    """What a tariff says, before it becomes LaneTariff rows: from the code parser or from the model."""

    carrier: str | None = None
    currency: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    lanes: dict[tuple[str, str], dict] = field(default_factory=dict)  # transit, minimum, breaks, fuel, security
    surcharges: dict[tuple[str, str], tuple[Decimal, Decimal]] = field(default_factory=dict)


def _num(text: str) -> Decimal:
    try:
        return Decimal(text.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().split(" 00:00")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unknown date: {text!r}")


def _break_kg(match: re.Match) -> Decimal:
    start, plus, sign, upto = match.groups()[:4]
    if sign == "-":
        return Decimal(0)
    return Decimal(start or plus or upto)


def _cell(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value).strip()


def _lines(filename: str, content: bytes) -> list[str]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        return [
            "  ".join(_cell(v) for v in row if v is not None and str(v).strip())
            for sheet in book.worksheets
            for row in sheet.iter_rows(values_only=True)
        ]
    if suffix == ".docx":
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(io.BytesIO(content))
        lines = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                lines.append(Paragraph(child, document).text)
            elif child.tag.endswith("}tbl"):
                lines += ["  ".join(c.text.strip() for c in row.cells) for row in Table(child, document).rows]
        return lines
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if not text.strip():
            raise ValueError("pdf without text: scanned")
        return text.splitlines()
    if suffix in (".txt", ".csv"):
        try:
            return content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return content.decode("latin-1").splitlines()
    raise ValueError(f"no code parser for {suffix or filename}")


def _codes(text: str, currency: str | None) -> list[str]:
    return [c for c in CODE.findall(text) if c != currency]


def _read(lines: list[str]) -> Sheet:
    sheet = Sheet()
    origin: str | None = None
    breaks: list[Decimal] = []
    surcharge_columns = False
    block: tuple[str, str] | None = None
    lead = ["transit", "minimum"]
    for raw in lines:
        line = raw.strip()
        lower = line.lower()
        if not line:
            continue
        if sheet.carrier is None:
            sheet.carrier = line.split("|")[0].strip() if "|" in line else ""
            if sheet.carrier:
                continue
        if m := re.match(r"currency\b[\s:|]*([A-Z]{3})\b", line, re.IGNORECASE):
            sheet.currency = m.group(1).upper()
            continue
        if m := re.match(r"valid\s+from\b[\s:|]*(.+)$", line, re.IGNORECASE):
            sheet.valid_from = _date(m.group(1))
            continue
        if m := re.match(r"valid\s+(?:to|until)\b[\s:|]*(.+)$", line, re.IGNORECASE):
            sheet.valid_to = _date(m.group(1))
            continue
        if lower.startswith("origin"):
            codes = _codes(line, sheet.currency)
            origin = codes[-1] if codes else None
            continue
        named = [m.lastgroup for m in LEAD_COLUMN.finditer(line)]
        if sorted(named) == ["minimum", "transit"] and not re.search(r"\d", BREAK_TOKEN.sub("", line)):
            lead = named
        tokens = list(BREAK_TOKEN.finditer(line))
        if len(tokens) >= 2:
            breaks = [_break_kg(t) for t in tokens]
            surcharge_columns = "fuel" in lower and "security" in lower
            block = None
            continue
        if "fuel" in lower and "security" in lower and ":" in line:
            lane_text, _, rest = line.partition(":")
            lane = _lane(_codes(lane_text, sheet.currency), origin)
            fuel = re.search(rf"fuel\D*?({NUM})", rest, re.IGNORECASE)
            security = re.search(rf"security\D*?({NUM})", rest, re.IGNORECASE)
            if not (lane and fuel and security):
                raise ValueError(f"unreadable surcharge line: {line!r}")
            sheet.surcharges[lane] = (_num(fuel.group(1)), _num(security.group(1)))
            continue
        if block and (m := BREAK_LINE.match(line)):
            sheet.lanes[block]["breaks"].append((_break_kg(m), _num(m.group(5))))
            continue
        if block and (m := re.match(rf"(transit|minimum|fuel|security)\b\D*?({NUM})", line, re.IGNORECASE)):
            key = m.group(1).lower()
            sheet.lanes[block][key] = int(_num(m.group(2))) if key == "transit" else _num(m.group(2))
            continue
        prefix = re.split(r"\d", line, maxsplit=1)[0]
        lane = _lane(_codes(prefix, sheet.currency), origin)
        if not lane:
            continue
        numbers = re.findall(NUM, line[len(prefix) :])
        if not numbers:
            block = lane
            sheet.lanes[lane] = {"breaks": []}
            continue
        expected = 2 + len(breaks) + (2 if surcharge_columns else 0)
        if not breaks or len(numbers) != expected:
            raise ValueError(f"row with {len(numbers)} numbers, expected {expected}: {line!r}")
        values = [_num(n) for n in numbers]
        row = dict(zip(lead, values[:2], strict=True)) | {"breaks": list(zip(breaks, values[2:], strict=False))}
        row["transit"] = int(row["transit"])
        if surcharge_columns:
            row["fuel"], row["security"] = values[-2], values[-1]
        sheet.lanes[lane] = row
        block = None
    return sheet


def _lane(codes: list[str], origin: str | None) -> tuple[str, str] | None:
    if len(codes) >= 2:
        return codes[0], codes[1]
    if len(codes) == 1 and origin:
        return origin, codes[0]
    return None


def _carrier_from(filename: str) -> str:
    stem = re.split(r"-tariff", Path(filename).stem, maxsplit=1)[0]
    return stem.replace("-", " ").replace("_", " ").title()


def _build(sheet: Sheet, filename: str, digest: str) -> list[LaneTariff]:
    if not (sheet.currency and sheet.valid_from and sheet.valid_to):
        raise ValueError("currency or validity missing")
    if not sheet.lanes:
        raise ValueError("no lanes found")
    if stray := set(sheet.surcharges) - set(sheet.lanes):
        raise ValueError(f"surcharges without rates: {sorted(stray)}")
    tariffs = []
    for (origin, destination), row in sheet.lanes.items():
        fuel, security = sheet.surcharges.get((origin, destination), (row.get("fuel"), row.get("security")))
        if fuel is None or security is None or "transit" not in row or "minimum" not in row:
            raise ValueError(f"{origin}-{destination}: transit, minimum or surcharges missing")
        tariffs.append(
            LaneTariff(
                carrier=sheet.carrier or _carrier_from(filename),
                origin=origin,
                destination=destination,
                currency=sheet.currency,
                valid_from=sheet.valid_from,
                valid_to=sheet.valid_to,
                version=f"{sheet.valid_from.isoformat()}#{digest}",
                transit_days=row["transit"],
                minimum=row["minimum"],
                breaks=tuple(row["breaks"]),
                fuel_per_kg=fuel,
                security_per_kg=security,
                source=filename,
            )
        )
    return tariffs


def _digest(*contents: bytes) -> str:
    return hashlib.sha256(b"".join(contents)).hexdigest()[:8]


def parse(filename: str, content: bytes) -> list[LaneTariff]:
    """Code parsers per file type (xlsx, docx, txt, text pdf). Raises ValueError when the layout is unknown."""
    return _build(_read(_lines(filename, content)), filename, _digest(content))


PAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["carrier", "currency", "valid_from", "valid_to", "breaks_kg", "lanes", "surcharges"],
    "properties": {
        "carrier": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "valid_from": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "valid_to": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "breaks_kg": {"type": "array", "items": {"type": "string"}, "description": "lower kg of each rate column"},
        "lanes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["origin", "destination", "transit_days", "minimum", "rates"],
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "transit_days": {"type": "string"},
                    "minimum": {"type": "string"},
                    "rates": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "surcharges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["origin", "destination", "fuel", "security"],
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "fuel": {"type": "string"},
                    "security": {"type": "string"},
                },
            },
        },
    },
}

PAGE_PROMPT = (
    "You transcribe one page of an air freight tariff into JSON. Copy every number exactly as printed, one string "
    "per cell, keeping decimal commas; never compute, round or guess. Lanes are IATA airport codes. breaks_kg are "
    "the lower weight of each rate column in order (a column '-45' starts at 0). Use null or an empty list for "
    "what this page does not show."
)


async def read_with_model(ctx: Context, filename: str, pages: tuple[bytes, ...]) -> list[LaneTariff]:
    """Images, scanned PDFs and layouts `parse` refuses: one `llm.ask_json` call per page."""
    sheet = Sheet()
    breaks: list[Decimal] = []
    for number, page in enumerate(pages, 1):
        result = await llm.ask_json(
            ctx,
            purpose="tariff.read",
            system=PAGE_PROMPT,
            user=f"File {filename}, page {number} of {len(pages)}.",
            schema=PAGE_SCHEMA,
            images=(page,),
            max_output_tokens=3000,
        )
        data = result.data
        sheet.carrier = sheet.carrier or data["carrier"]
        sheet.currency = sheet.currency or (data["currency"] or "").upper() or None
        sheet.valid_from = sheet.valid_from or (_date(data["valid_from"]) if data["valid_from"] else None)
        sheet.valid_to = sheet.valid_to or (_date(data["valid_to"]) if data["valid_to"] else None)
        breaks = [_num(b) for b in data["breaks_kg"]] or breaks
        for lane in data["lanes"]:
            if len(lane["rates"]) != len(breaks):
                raise ValueError(
                    f"{lane['origin']}-{lane['destination']}: {len(lane['rates'])} rates, {len(breaks)} breaks"
                )
            sheet.lanes[(lane["origin"].upper(), lane["destination"].upper())] = {
                "transit": int(_num(re.search(NUM, lane["transit_days"]).group())),
                "minimum": _num(lane["minimum"]),
                "breaks": list(zip(breaks, (_num(r) for r in lane["rates"]), strict=True)),
            }
        for s in data["surcharges"]:
            sheet.surcharges[(s["origin"].upper(), s["destination"].upper())] = (_num(s["fuel"]), _num(s["security"]))
    return _build(sheet, filename, _digest(*pages))


def check(tariffs: list[LaneTariff]) -> list[str]:
    """Code checks on every parse: rates fall as weight rises, minimum > 0 and no transit days, lane codes known,
    dates sane. A sheet with findings is stored as failed, not used for pricing."""
    if not tariffs:
        return ["no lanes"]
    findings = []
    seen = set()
    for t in tariffs:
        lane = f"{t.origin}-{t.destination}"
        if (
            not (re.fullmatch("[A-Z]{3}", t.origin) and re.fullmatch("[A-Z]{3}", t.destination))
            or t.origin == t.destination
        ):
            findings.append(f"{lane}: lane codes")
        if (t.carrier, t.origin, t.destination) in seen:
            findings.append(f"{lane}: twice")
        seen.add((t.carrier, t.origin, t.destination))
        if not re.fullmatch("[A-Z]{3}", t.currency):
            findings.append(f"{lane}: currency {t.currency!r}")
        if t.valid_from > t.valid_to:
            findings.append(f"{lane}: valid from after valid to")
        if t.minimum <= 0:
            findings.append(f"{lane}: minimum {t.minimum}")
        elif t.breaks and t.minimum <= 30 and t.minimum == int(t.minimum) and t.minimum < 2 * t.breaks[0][1]:
            findings.append(f"{lane}: minimum {t.minimum} looks like transit days")  # columns read swapped
        if not 0 < t.transit_days <= 30:
            findings.append(f"{lane}: transit {t.transit_days}")
        if not t.breaks or t.breaks[0][0] != 0:
            findings.append(f"{lane}: first break not at 0 kg")
        starts = [k for k, _ in t.breaks]
        rates = [r for _, r in t.breaks]
        if starts != sorted(set(starts)):
            findings.append(f"{lane}: breaks not ascending")
        if any(r <= 0 for r in rates) or any(b > a for a, b in itertools.pairwise(rates)):
            findings.append(f"{lane}: rates must be positive and fall as weight rises")
        if t.fuel_per_kg < 0 or t.security_per_kg < 0 or (rates and t.fuel_per_kg + t.security_per_kg >= rates[0]):
            findings.append(f"{lane}: surcharges {t.fuel_per_kg}/{t.security_per_kg}")
    return findings


def _pdf_pages(content: bytes) -> tuple[bytes, ...]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = []
        for page in pdf.pages:
            image = page.to_image(resolution=144).original
            with io.BytesIO() as stream:
                image.convert("RGB").save(stream, format="JPEG")
                pages.append(stream.getvalue())
        return tuple(pages)


async def _ingest(ctx: Context, source: str, files: list[tuple[str, bytes]], session_id: int | None = None) -> None:
    filename = files[0][0]
    try:
        if Path(filename).suffix.lower() in IMAGES:
            if store.has_source(ctx, source):
                return
            tariffs = await read_with_model(ctx, source, tuple(content for _, content in files))
        elif Path(filename).suffix.lower() == ".pdf":
            content = files[0][1]
            try:
                tariffs = await asyncio.to_thread(parse, source, content)
            except ValueError:
                if store.has_source(ctx, source):
                    return
                pages = await asyncio.to_thread(_pdf_pages, content)
                tariffs = await read_with_model(ctx, source, pages)
        else:
            tariffs = parse(source, files[0][1])
        findings = check(tariffs)
    except (ValueError, KeyError, TypeError, AttributeError, ApiError, httpx.HTTPError, llm.BudgetExceeded) as exc:
        tariffs, findings = [], [f"{type(exc).__name__}: {exc}"]
    if findings:
        events.emit(
            ctx, "tariff.failed", "tariffs", session_id=session_id, payload={"source": source, "findings": findings}
        )
        return
    store.upsert(ctx, tariffs)
    events.emit(
        ctx,
        "tariff.ingested",
        "tariffs",
        session_id=session_id,
        payload={"source": source, "carrier": tariffs[0].carrier, "version": tariffs[0].version, "lanes": len(tariffs)},
    )


def _documents(files: list[tuple[str, bytes]]) -> dict[str, list[tuple[str, bytes]]]:
    """Pages of one scan (`name-p1.jpg`, `name-p2.jpg`) become one document, in page order."""
    documents: dict[str, list[tuple[str, bytes]]] = {}
    for name, content in sorted(files):
        path = Path(name)
        key = re.sub(r"-p\d+$", "", path.stem) + path.suffix.lower() if path.suffix.lower() in IMAGES else name
        documents.setdefault(key, []).append((name, content))
    return documents


def _reference_csv(ctx: Context, content: bytes) -> bool:
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    columns = set(rows[0]) if rows else set()
    if {"name", "kind", "email", "serves"} <= columns:
        store.save_carriers(ctx, rows)
    elif {"zone", "area", "airport"} <= columns:
        store.save_zones(ctx, rows)
    else:
        return False
    return True


async def handle(ctx: Context, message: Message) -> None:
    """A tariff mail: download attachments, ingest each, emit `tariff.ingested` or `tariff.failed`."""
    files = [(a.filename, await ctx.mailbox.attachment(message.id, a.id)) for a in message.attachments]
    for key, document in _documents(files).items():
        if key.lower().endswith(".csv") and _reference_csv(ctx, document[0][1]):
            continue
        await _ingest(ctx, f"mail:{message.id}:{key}", document, message.session_id)


async def ingest_folder(ctx: Context, folder: Path) -> None:
    """The start kit: every tariff file, plus the carrier list and pickup zones, into `pricing/store.py`."""
    files = [(p.name, p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()]
    for key, document in _documents(files).items():
        if key.lower().endswith(".csv"):
            _reference_csv(ctx, document[0][1])
            continue
        await _ingest(ctx, key, document)
