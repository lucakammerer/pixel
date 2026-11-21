import asyncio
import csv
import hashlib
import io
import itertools
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from pixel import llm
from pixel.api.client import ApiError
from pixel.contracts import Attachment, LaneTariff, Message
from pixel.core import events
from pixel.core.context import Context
from pixel.pricing import bids, store

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
AUDIO = {".mp3", ".wav", ".m4a", ".ogg"}
READABLE = {".xlsx", ".docx", ".pdf", ".txt", ".csv", *AUDIO}
SPEECH_MODEL = "base"  # multilingual: a German voice message too; about 1 s per minute of speech on four cores
# Spoken prices for the bid and request rules: "340 euros and 60 cents" is 340.60 EUR.
SPOKEN_CENTS = re.compile(r"(\d[\d,.]*)\s*euros?,?\s+(?:and|und)\s+(\d{1,2})\s*cents?\b", re.IGNORECASE)
SPOKEN_EUR = re.compile(r"(\d[\d,.]*)\s*euros?\b", re.IGNORECASE)
# Whisper writes small numbers as words: "four hours", "twelve pieces", "vier Stunden" become digits for the rules.
ONES = [
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
ONES_DE = [
    "eins",
    "zwei",
    "drei",
    "vier",
    "fünf",
    "sechs",
    "sieben",
    "acht",
    "neun",
    "zehn",
    "elf",
    "zwölf",
    "dreizehn",
    "vierzehn",
    "fünfzehn",
    "sechzehn",
]
ONES_DE += ["siebzehn", "achtzehn", "neunzehn"]
TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
TENS_DE = ["zwanzig", "dreißig", "vierzig", "fünfzig", "sechzig", "siebzig", "achtzig", "neunzig"]
NUMBER_WORDS = {w: i + 1 for words in (ONES, ONES_DE) for i, w in enumerate(words)} | {"ein": 1, "eine": 1, "einen": 1}
NUMBER_WORDS |= {w: 10 * (i + 2) for words in (TENS, TENS_DE) for i, w in enumerate(words)}
_ones = "|".join(sorted((w for w, n in NUMBER_WORDS.items() if n < 20), key=len, reverse=True))
_tens = "|".join(w for w, n in NUMBER_WORDS.items() if n >= 20)
SPOKEN_NUMBER = re.compile(
    rf"\b(?:(?P<one_de>{_ones})und(?P<ten_de>{_tens})|(?P<ten>{_tens})(?:[\s-]+(?P<unit>{_ones}))?|(?P<one>{_ones}))"
    r"(?=\s+(?:working\s+|business\s+)?(?:hours?|hrs?|days?|pieces?|pallets?|boxes|cartons?|crates?|packages?|colli"
    r"|stunden|tagen?|werktagen?|stück|paletten|kisten|kartons?|packstücke?)\b)",
    re.IGNORECASE,
)
_speech: list = []
_speech_lock = threading.Lock()
VALIDITY = re.compile(r"valid(?:ity)?(?:\s+from)?\b[\s:|]*(.+?)\s+(?:-|–|—|to|until|bis)\s+(.+)$", re.IGNORECASE)
RATE_UNIT = re.compile(r"\brates\s+are\s+(?:quoted\s+)?per\s+(\d+)?\s*kg\b", re.IGNORECASE)  # "per 100 kg" divides
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y")


class Unreadable(ValueError):
    """A file code holds no text reader for: video, an image, an unknown type, a scan without text."""


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


def _span(start: str, end: str) -> tuple[date, date] | None:
    """Both ends of a validity written on one line, or None when either is not a date."""
    try:
        return _date(start), _date(end)
    except ValueError:
        return None


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
            raise Unreadable("pdf without text: scanned")
        return text.splitlines()
    if suffix in (".txt", ".csv"):
        try:
            return content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return content.decode("latin-1").splitlines()
    if suffix in AUDIO:
        return _spoken(_transcribe(content))
    raise Unreadable(f"no code parser for {suffix or filename}")


async def attachment_lines(ctx: Context, message: Message, attachment: Attachment, actor: str) -> list[str] | None:
    """The text lines of a customer's or carrier's attachment. A file code cannot read (a video, a scan) is a
    deliberate `attachment.unreadable`, never an `error`: the mail goes on as if that file held nothing."""
    suffix = Path(attachment.filename).suffix.lower()
    try:
        if suffix not in READABLE:
            raise Unreadable(f"no code parser for {suffix or attachment.filename}")
        content = await ctx.mailbox.attachment(message.id, attachment.id)
        return await asyncio.to_thread(_lines, attachment.filename, content)  # a voice message takes seconds
    except Unreadable as e:
        events.emit(ctx, "attachment.unreadable", actor, session_id=message.session_id, message_id=message.id,
                    payload={"file": attachment.filename, "type": attachment.content_type, "reason": str(e)})  # fmt: skip
    except Exception as e:  # noqa: BLE001  a broken file must not lose the mail
        events.emit(ctx, "error", actor, session_id=message.session_id, message_id=message.id,
                    payload={"where": f"{actor}_attachment", "file": attachment.filename, "error": type(e).__name__})  # fmt: skip
    return None


def speech_model():
    """The local speech model, loaded once. The app loads it at start, so no request waits for its download."""
    from faster_whisper import WhisperModel

    with _speech_lock:
        if not _speech:
            _speech.append(WhisperModel(SPEECH_MODEL, device="cpu", compute_type="int8"))
        return _speech[0]


def _transcribe(content: bytes) -> list[str]:
    """A voice message as one line per spoken sentence, by a local model: no gateway spend. Slow: run it in a thread."""
    import av
    import numpy as np

    with av.open(io.BytesIO(content)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        pcm = [f.to_ndarray() for frame in container.decode(audio=0) for f in resampler.resample(frame)]
    audio = np.concatenate(pcm, axis=1).flatten().astype(np.float32) / 32768
    model = speech_model()
    with _speech_lock:
        segments, _ = model.transcribe(audio, beam_size=1)
        text = " ".join(segment.text.strip() for segment in segments)
    # Segments break mid-sentence ("Collection at Postcode" / "51943 ..."): a sentence is one line.
    return re.split(r"(?<=[.!?])\s+", text)


# "one hundred and twenty boxes": the words before the last one are part of the number, so none of it is converted.
LARGER = re.compile(r"\b(?:hundred|thousand|hundert|tausend)(?:\s+(?:and|und))?[\s-]*$", re.IGNORECASE)


def _number(m: re.Match) -> str:
    if LARGER.search(m.string[: m.start()]):
        return m[0]
    if m["one_de"]:
        return str(NUMBER_WORDS[m["ten_de"].lower()] + NUMBER_WORDS[m["one_de"].lower()])
    if m["ten"]:
        return str(NUMBER_WORDS[m["ten"].lower()] + (NUMBER_WORDS[m["unit"].lower()] if m["unit"] else 0))
    return str(NUMBER_WORDS[m["one"].lower()])


def _spoken(lines: list[str]) -> list[str]:
    lines = [SPOKEN_NUMBER.sub(_number, line) for line in lines]
    return [
        SPOKEN_EUR.sub(r"\1 EUR", SPOKEN_CENTS.sub(lambda m: f"{m[1]}.{int(m[2]):02d} EUR", line)) for line in lines
    ]


def _codes(text: str, currency: str | None) -> list[str]:
    return [c for c in CODE.findall(text) if c != currency]


def _lead(line: str) -> list[str] | None:
    named = [m.lastgroup for m in LEAD_COLUMN.finditer(line)]
    return (
        named if sorted(named) == ["minimum", "transit"] and not re.search(r"\d", BREAK_TOKEN.sub("", line)) else None
    )


def _read(lines: list[str]) -> Sheet:
    sheet = Sheet()
    origin: str | None = None
    breaks: list[Decimal] = []
    surcharge_columns = False
    block: tuple[str, str] | None = None
    lead = ["transit", "minimum"]
    per_kg = Decimal(1)
    above = ""
    for raw in lines:
        line = raw.strip()
        lower = line.lower()
        previous, above = above, line
        if not line:
            continue
        if sheet.carrier is None:
            sheet.carrier = line.split("|")[0].strip() if "|" in line else ""
            if sheet.carrier:
                continue
        if m := RATE_UNIT.search(line):
            per_kg = Decimal(m.group(1) or 1)
            continue
        if m := re.match(r"currency\b[\s:|]*([A-Z]{3})\b", line, re.IGNORECASE):
            sheet.currency = m.group(1).upper()
            continue
        if (m := VALIDITY.match(line)) and (span := _span(m.group(1), m.group(2))):
            sheet.valid_from, sheet.valid_to = span
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
        tokens = list(BREAK_TOKEN.finditer(line))
        if len(tokens) >= 2:
            # The header orders the columns: on the breaks' line or right above it, never a note or legend sentence.
            if named := _lead(line) or (not re.search(r"[;:]|\.$", previous) and _lead(previous)):
                lead = named
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
            sheet.lanes[block]["breaks"].append((_break_kg(m), _num(m.group(5)) / per_kg))
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
        row = dict(zip(lead, values[:2], strict=True)) | {
            "breaks": [(b, r / per_kg) for b, r in zip(breaks, values[2:], strict=False)]
        }
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


def _stated_fx(files: list[tuple[str, bytes]], currency: str, mail: str) -> Decimal | None:
    """EUR per unit of a non-EUR sheet's currency as the sheet or its mail states it ("1 USD = 0.92 EUR")."""
    if currency == "EUR":
        return None
    for name, content in files:
        try:
            if fx := bids._fx("\n".join(_lines(name, content)), currency):
                return fx
        except ValueError:  # a scan or image: no text to read
            continue
    return bids._fx(mail, currency)


async def _ingest(
    ctx: Context, source: str, files: list[tuple[str, bytes]], session_id: int | None = None, mail: str = ""
) -> None:
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
    payload = {"source": source, "carrier": tariffs[0].carrier, "version": tariffs[0].version, "lanes": len(tariffs)}
    if fx := _stated_fx(files, tariffs[0].currency, mail):
        payload["fx_eur"] = str(fx)  # read by `pricing/engine.py` `_fx_eur`
    events.emit(ctx, "tariff.ingested", "tariffs", session_id=session_id, payload=payload)


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
        await _ingest(ctx, f"mail:{message.id}:{key}", document, message.session_id, message.body)


async def ingest_folder(ctx: Context, folder: Path) -> None:
    """The start kit: every tariff file, plus the carrier list and pickup zones, into `pricing/store.py`."""
    files = [(p.name, p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()]
    for key, document in _documents(files).items():
        if key.lower().endswith(".csv"):
            _reference_csv(ctx, document[0][1])
            continue
        await _ingest(ctx, key, document)
