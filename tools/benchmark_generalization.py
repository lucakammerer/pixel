"""Generalization benchmark: how well the code holds up across tariff layouts and request phrasings.

Usage:
    uv run python -m tools.benchmark_generalization          # every check, exit 1 on a failure
    uv run python -m tools.benchmark_generalization --gaps   # only the open known gaps, grouped by owner
    uv run python -m tools.benchmark_generalization --stub   # an empty rfqs/live.jsonl row per new captured request

Runs offline and free on the fixtures in `tests/fixtures/` and the sheets in `data/start-kit/`:

1. Hand prices: every row of `tariffs/hand_prices.csv` against `pricing.calc.price_tariff`.
2. Tariff sources: every row of `tariffs/source_review.csv` parses to its lane count with no `check()` findings.
3. Rewrites: which rows of `rfqs/rewrites.jsonl` the rules extract, whether they read every shipment field right,
   and which need the model.
4. Partial cases: the rows of `rfqs/partial_cases.jsonl` that have an invariant to check.
5. Changed layouts: every sheet in `tariffs/variants/` is parsed right or refused, never parsed into wrong values.
6. Routing: the router's rules on `mails/routing_probes.jsonl`.
7. Carrier bids: `pricing.bids.parse` on `mails/bid_probes.jsonl`.
8. Spreadsheet changes: layout-only edits of every kit xlsx parse to the original's tariffs or are refused.
9. Captured requests: every `mails/rfq-*.json` read by the rules against the fields a person read from the mail
   (`rfqs/live.jsonl`), with the score per field.
10. The same requests changed in ways seen live (an old request quoted below, a reply's subject, pounds): each
   read right or left to the model.
11. Mail against its attached document (rounds 33 to 35): every figure the document states, changed in the mail,
   in every column order, file format and document layout (`tools/document_conflicts.py`); the document's figure
   is the right reading.

Rows carrying `known_gap` are reported as gaps for the code owner instead of failures.

Scanned sheets are read by the real `agents.tariffs.agent.read_with_model`, with the model's answer replayed from
`tariffs/<carrier>-model-pages.json` instead of a gateway call. Exits 1 when any check fails.
`tests/test_generalization.py` asserts on the same functions.
"""

import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pixel import llm
from pixel.agents.rfq import extract
from pixel.agents.router import agent as router
from pixel.agents.tariffs import agent
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LaneTariff, LlmResult, MailKind, Message, Preference, Rfq
from pixel.core import db, poller
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.core.outbox import _store
from pixel.pricing import bids
from pixel.pricing.calc import chargeable_kg, price_tariff
from tools import document_conflicts

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
TARIFF_FIXTURES = FIXTURES / "tariffs"
RFQ_FIXTURES = FIXTURES / "rfqs"
START_KIT = ROOT / "data" / "start-kit"
NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)

# Partial-case categories where the rules must return nothing rather than guess.
NO_GUESS = {"critical_ambiguity", "insufficient_critical_facts", "conflicting_facts"}


def _deadline():
    return deadline_for(NOW, settings())


def _rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _message(subject: str, body: str) -> Message:
    return Message(
        id=1,
        session_id=1,
        direction="in",
        sender="buyer@example.com",
        to="pixel@example.com",
        subject=subject,
        body=body,
        sent_at=NOW,
    )


async def _replay_scan(source: str, pages: tuple[bytes, ...]) -> list[LaneTariff]:
    """The real scan reader, with the model's per-page answers replayed from the stored transcription."""
    transcript = TARIFF_FIXTURES / f"{source.split('-')[0]}-model-pages.json"
    if not transcript.exists():
        raise ValueError(f"no stored transcription {transcript.name} for scan {source}")
    answers = iter(json.loads(transcript.read_text()))

    async def replay(ctx, **kw):
        return LlmResult(data=next(answers), model="replay", tokens_in=0, tokens_out=0, cost_usd=D(0))

    with patch.object(llm, "ask_json", replay):
        return await agent.read_with_model(None, source, pages)


def load_start_kit(folder: Path = START_KIT) -> tuple[dict[str, list[LaneTariff]], dict[str, str]]:
    """Every tariff in the folder, keyed by each file name it came from, plus the error per file that failed."""
    files = [(p.name, p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()]
    tariffs: dict[str, list[LaneTariff]] = {}
    errors: dict[str, str] = {}
    for source, document in agent._documents(files).items():
        if source.lower().endswith(".csv"):
            continue
        names = [name for name, _ in document]
        try:
            if Path(names[0]).suffix.lower() in agent.IMAGES:
                parsed = asyncio.run(_replay_scan(source, tuple(content for _, content in document)))
            else:
                parsed = agent.parse(source, document[0][1])
        except (ValueError, KeyError, TypeError) as exc:
            for name in names:
                errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        for name in names:
            tariffs[name] = parsed
    return tariffs, errors


@dataclass
class Report:
    passed: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)


def audit_hand_prices(tariffs: dict[str, list[LaneTariff]]) -> Report:
    """Every hand-calculated price must match `price_tariff` in total and billed weight."""
    report = Report()
    for r in _rows(TARIFF_FIXTURES / "hand_prices.csv"):
        report.total += 1
        origin, destination = r["lane"].split("-")
        lane = next(
            (t for t in tariffs.get(r["source_file"], []) if (t.origin, t.destination) == (origin, destination)),
            None,
        )
        if lane is None:
            report.failures.append(f"{r['case_id']}: lane {r['lane']} not found in {r['source_file']}")
            continue
        rfq = Rfq(
            session_id=1,
            message_id=1,
            customer="bench",
            origin=origin,
            destination=destination,
            weight_kg=D(r["actual_kg"]),
            volume_cbm=D(r["volume_cbm"]),
            received_at=NOW,
            deadline_at=NOW,
        )
        option = price_tariff(lane, rfq)
        if option.total == D(r["total_eur"]) and option.billed_kg == D(r["billed_kg"]):
            report.passed += 1
        else:
            report.failures.append(
                f"{r['case_id']}: got {option.total} billed on {option.billed_kg} kg, "
                f"expected {r['total_eur']} billed on {r['billed_kg']} kg"
            )
    return report


def audit_sources(tariffs: dict[str, list[LaneTariff]], errors: dict[str, str]) -> tuple[Report, int]:
    """Every reviewed source parses to its lane count with no findings; every kit sheet has a review row."""
    report = Report()
    lanes = 0
    reviewed: set[str] = set()
    for r in _rows(TARIFF_FIXTURES / "source_review.csv"):
        report.total += 1
        names = [n.strip() for n in r["file"].split(";")]
        reviewed.update(names)
        name = names[0]
        if name in errors:
            report.failures.append(f"{r['file']}: {errors[name]}")
            continue
        parsed = tariffs.get(name)
        if parsed is None:
            report.failures.append(f"{r['file']}: not in the start kit")
            continue
        findings = agent.check(parsed)
        if findings:
            report.failures.append(f"{r['file']}: {len(findings)} check findings, first: {findings[0]}")
        elif len(parsed) != int(r["source_lane_count"]):
            report.failures.append(f"{r['file']}: {len(parsed)} lanes parsed, {r['source_lane_count']} in the source")
        else:
            report.passed += 1
            lanes += len(parsed)
    for name in sorted((set(tariffs) | set(errors)) - reviewed):
        report.failures.append(f"{name}: in the start kit but has no row in source_review.csv")
    return report, lanes


@dataclass
class Rewrite:
    case_id: str
    by_rules: bool
    right: bool | None  # None when the rules gave nothing and the model has to read it
    wrong_fields: list[str] = field(default_factory=list)
    known_gap: str | None = None  # the row's handoff note: a wrong answer here is reported, not failed


def _wrong_fields(rfq: Rfq, exp: dict) -> list[str]:
    """The shipment fields the rules read differently from the reference, as `field: got != expected`."""
    pairs = {
        "origin": (rfq.origin, exp["origin"]),
        "destination": (rfq.destination, exp["destination"]),
        "weight_kg": (rfq.weight_kg, D(exp["weight_kg"] or 0)),
        "volume_cbm": (rfq.volume_cbm, D(exp["volume_cbm"] or 0)),
        "pieces": (rfq.pieces, exp["pieces"]),
        "preference": (rfq.preference.value, exp["preference"]),
        "door_pickup": (rfq.door_pickup, exp["door_pickup"]),
        "pickup_postcode": (rfq.pickup_postcode, exp["pickup_postcode"]),
    }
    return [f"{k}: {got} != {want}" for k, (got, want) in pairs.items() if got != want]


def audit_rewrites() -> list[Rewrite]:
    """Per rewrite: did the rules extract it, and if so, did they read every shipment field right.

    A row may carry `known_gap`, a note for the code owner on what the rules miss; its wrong answer is reported as
    a gap instead of a failure, so the corpus can hold the right answer before the code does."""
    results = []
    deadline = _deadline()
    for item in _jsonl(RFQ_FIXTURES / "rewrites.jsonl"):
        gap = item.get("known_gap")
        rfq = extract.by_rules(_message("Rate request", item["text"]), deadline)
        if rfq is None:
            results.append(Rewrite(item["case_id"], by_rules=False, right=None, known_gap=gap))
            continue
        wrong = _wrong_fields(rfq, item["expected"])
        results.append(Rewrite(item["case_id"], True, not wrong, wrong, gap))
    return results


def audit_partial_cases() -> tuple[Report, int]:
    """The rules must not guess where facts are missing, ambiguous or conflicting, and must hear urgency; a
    request with a volume but no weight is priced on its volume or the weight is asked, never invented.

    Returns the report over the cases with an invariant, and how many cases have none yet.
    """
    report = Report()
    unchecked = 0
    deadline = _deadline()
    for item in _jsonl(RFQ_FIXTURES / "partial_cases.jsonl"):
        rfq = extract.by_rules(_message(item["message"]["subject"], item["message"]["body"]), deadline)
        if item["category"] in NO_GUESS:
            report.total += 1
            if rfq is None:
                report.passed += 1
            else:
                report.failures.append(f"{item['case_id']}: rules guessed a lane or weight ({item['category']})")
        elif item["expected"].get("priceable") and item["expected"]["weight_kg"] is None:
            # Volume only: the model reads the lane and pieces and leaves weight and volume empty, as its prompt
            # forbids calculating; code must find the volume in the text and price on it.
            report.total += 1
            exp = item["expected"]
            answer = {k: exp.get(k) for k in ("origin", "destination", "pieces", "preference", "door_pickup")}
            answer |= {"weight_kg": None, "volume_cbm": None, "pickup_postcode": None, "currency": None}
            answer["language"] = "en"
            message = _message(item["message"]["subject"], item["message"]["body"])
            rfq = extract._from_model(answer, message, deadline)
            chargeable = chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
            # Priced on the volume, or the weight asked from the customer: both are fine. Inventing a gross
            # weight, or reading a wrong volume, is not.
            priced = "weight_kg" not in rfq.missing and chargeable == D(exp["chargeable_kg"])
            asked = "weight_kg" in rfq.missing and rfq.volume_cbm * 167 == D(exp["chargeable_kg"])
            if rfq.weight_kg == 0 and (priced or asked):
                report.passed += 1
            else:
                report.failures.append(
                    f"{item['case_id']}: missing {list(rfq.missing)}, gross {rfq.weight_kg}, "
                    f"chargeable {chargeable}, expected {exp['chargeable_kg']}"
                )
        elif item["case_id"] == "urgency_selects_fastest":
            report.total += 1
            if rfq is not None and rfq.preference == Preference.FASTEST:
                report.passed += 1
            else:
                report.failures.append(f"{item['case_id']}: urgency did not select fastest")
        else:
            unchecked += 1
    return report, unchecked


@dataclass
class Live:
    fixture: str
    by_rules: bool
    wrong_fields: list[str] = field(default_factory=list)
    judged: list[str] = field(default_factory=list)  # the fields its row states, the ones compared
    known_gap: str | None = None
    todo: str | None = None  # the row is missing or unfinished: reported, never a failure (a capture must not block)

    @property
    def right(self) -> bool | None:
        return None if not self.by_rules or self.todo else not self.wrong_fields


LIVE_FIELDS = ("origin", "destination", "weight_kg", "volume_cbm", "pieces", "preference", "door_pickup",
               "pickup_postcode", "currency", "language")  # fmt: skip
MAILS = FIXTURES / "mails"
UNREAD = "?"  # a field of a stub row nobody has read from the mail yet


def _live_value(rfq: Rfq, key: str):
    value = getattr(rfq, key)
    return value.value if isinstance(value, Preference) else value


def _live_want(key: str, want):
    return D(want) if key in ("weight_kg", "volume_cbm") and want is not None else want


def audit_live() -> list[Live]:
    """Every captured customer request (`mails/rfq-*.json`) against `rfqs/live.jsonl`: the fields a person read
    from the mail itself, never from the code's output. A field the row leaves out is not judged (the origin of a
    door-to-port request is the pickup zone's airport, set by `extract.extract`, not by the rules). None from the
    rules is safe: the model reads the mail then. A captured request without a row, or with a field still "?", is
    a to-do, not a failure: capturing a mail must never turn the tests red and stop the host's deploy job. A key the
    row names that is no field is a failure: it would leave that field unjudged without a word."""
    rows = {r["fixture"]: r for r in _jsonl(RFQ_FIXTURES / "live.jsonl")}
    deadline = _deadline()
    results = []
    for path in sorted(MAILS.glob("rfq-*.json")):
        row = rows.get(path.name)
        if row is None:
            results.append(Live(path.name, by_rules=True, todo="write its row (--stub)"))
            continue
        if unknown := sorted(set(row["expected"]) - set(LIVE_FIELDS)):
            results.append(Live(path.name, True, [f"unknown field {k}" for k in unknown]))
            continue
        exp = {k: v for k, v in row["expected"].items() if v != UNREAD}
        if unread := [k for k, v in row["expected"].items() if v == UNREAD]:
            results.append(Live(path.name, True, todo=f"fill in {', '.join(unread)} from the mail"))
            continue
        rfq = extract.by_rules(to_message(json.loads(path.read_text()), "in"), deadline)
        if rfq is None:
            results.append(Live(path.name, False, judged=list(exp), known_gap=row.get("known_gap")))
            continue
        wrong = [f"{k}: {_live_value(rfq, k)} != {want}" for k, want in exp.items()
                 if _live_value(rfq, k) != _live_want(k, want)]  # fmt: skip
        results.append(Live(path.name, True, wrong, list(exp), row.get("known_gap")))
    return results


def live_by_field(results: list[Live]) -> dict[str, tuple[int, int]]:
    """Per field: right and judged among the requests the rules read."""
    tally: dict[str, list[int]] = {k: [0, 0] for k in LIVE_FIELDS}
    for r in results:
        if not r.by_rules or r.todo:
            continue
        wrong = {w.split(":", 1)[0] for w in r.wrong_fields}
        for k in r.judged:
            tally[k][1] += 1
            tally[k][0] += k not in wrong
    return {k: (right, total) for k, (right, total) in tally.items() if total}


# A wrong reading seen live becomes a change applied to every captured request, with an answer known in advance.
LIVE_MUTATIONS = RFQ_FIXTURES / "live-mutations.json"  # the old request and the reply subject, as seen live
LB = D("0.45359237")


def _mutations(raw: dict, exp: dict) -> dict[str, tuple[dict, dict]]:
    """The changed mails of one request and what a right reading gives for each: an older request quoted below
    (round 14), a reply's subject naming another lane, the weight written in pounds (21.11. 18:25)."""
    seen = json.loads(LIVE_MUTATIONS.read_text())
    found = {
        "quoted_old_request": (
            raw | {"body": raw["body"] + seen["quoted_old_request"], "subject": "RE: " + raw["subject"]},
            exp,
        ),
        "reply_subject": (raw | {"subject": seen["reply_subject"]}, exp),
    }
    weight = exp.get("weight_kg")
    figure = re.compile(rf"(?<![\d.,]){re.escape(weight)} kg\b") if weight else None
    if figure and len(figure.findall(raw["body"])) == 1:
        lbs = (D(weight) / LB).quantize(D(1))
        body = figure.sub(f"{lbs} lbs", raw["body"], count=1)
        found["pounds"] = (raw | {"body": body}, exp | {"weight_kg": str(lbs * LB)})
    return found


def audit_live_mutations() -> list[Live]:
    """Every captured request with a finished row, changed as `_mutations` says: read right or left to the model.
    A row's `known_gap_mutations` (`{"pounds": "<note> Owner: <role>."}`) names a change the rules misread today:
    reported as a gap for that owner, not failed."""
    rows = {r["fixture"]: r for r in _jsonl(RFQ_FIXTURES / "live.jsonl")}
    deadline, results = _deadline(), []
    for path in sorted(MAILS.glob("rfq-*.json")):
        row = rows.get(path.name)
        if row is None or UNREAD in row["expected"].values() or set(row["expected"]) - set(LIVE_FIELDS):
            continue
        exp = dict(row["expected"])
        gaps = row.get("known_gap_mutations") or {}
        for name, (raw, want) in _mutations(json.loads(path.read_text()), exp).items():
            case = f"{path.name} {name}"
            rfq = extract.by_rules(to_message(raw, "in"), deadline)
            if rfq is None:
                results.append(Live(case, False, judged=list(want), known_gap=gaps.get(name)))
                continue
            wrong = [f"{k}: {_live_value(rfq, k)} != {v}" for k, v in want.items()
                     if _live_value(rfq, k) != _live_want(k, v)]  # fmt: skip
            results.append(Live(case, True, wrong, list(want), gaps.get(name)))
    return results


def live_stubs() -> list[str]:
    """A row to fill in for every captured request that has none: every field "?", to be read from the mail by a
    person (null where the mail does not state it, the key removed where it is not to be judged). Never filled from
    what the code reads, or the corpus would only repeat today's misreads."""
    have = {r["fixture"] for r in _jsonl(RFQ_FIXTURES / "live.jsonl")}
    return [json.dumps({"fixture": p.name, "expected": dict.fromkeys(LIVE_FIELDS, UNREAD), "note": ""})
            for p in sorted(MAILS.glob("rfq-*.json")) if p.name not in have]  # fmt: skip


@dataclass
class Variant:
    file: str
    change: str
    # "parsed right"; "refused" (parse raised or check() found something); "not priced" (parsed, but price_tariff
    # raises, so the sheet is never used); "parsed wrong"
    outcome: str
    detail: str = ""
    safe: bool = True
    known_gap: str | None = None


def _spot_price(tariff: LaneTariff, kg: str) -> tuple[D, D]:
    rfq = Rfq(
        session_id=1,
        message_id=1,
        customer="bench",
        origin=tariff.origin,
        destination=tariff.destination,
        weight_kg=D(kg),
        received_at=NOW,
        deadline_at=NOW,
    )
    option = price_tariff(tariff, rfq)
    return option.total, option.billed_kg


def audit_variants() -> list[Variant]:
    """Synthetic sheets with one layout change each (`tariffs/variants/variants.jsonl`). A change may be parsed
    right or refused (then the model reads the sheet); a sheet parsed into wrong prices is never safe."""
    folder = TARIFF_FIXTURES / "variants"
    results = []
    for row in _jsonl(folder / "variants.jsonl"):
        gap = row.get("known_gap")
        try:
            parsed = agent.parse(row["file"], (folder / row["file"]).read_bytes())
        except ValueError as exc:
            outcome, detail = "refused", f"parse: {exc}"
        else:
            findings = agent.check(parsed)
            if findings:
                outcome, detail = "refused", f"check: {findings[0]}"
            else:
                wrong = []
                unpriced = ""
                if len(parsed) != row["lanes"]:
                    wrong.append(f"{len(parsed)} lanes, expected {row['lanes']}")
                first = parsed[0]
                for key in ("currency", "valid_from", "valid_to"):
                    if str(getattr(first, key)) != row[key]:
                        wrong.append(f"{key} {getattr(first, key)}, expected {row[key]}")
                lanes = {f"{t.origin}-{t.destination}": t for t in parsed}
                for name, facts in row["lane_facts"].items():
                    lane = lanes.get(name)
                    if lane and (lane.transit_days, lane.minimum) != (facts["transit_days"], D(facts["minimum"])):
                        wrong.append(
                            f"{name}: transit {lane.transit_days}, minimum {lane.minimum}, "
                            f"expected {facts['transit_days']} and {facts['minimum']}"
                        )
                for spot in row["spots"]:
                    lane = lanes.get(spot["lane"])
                    if lane is None:
                        wrong.append(f"{spot['lane']} missing")
                        continue
                    try:
                        total, billed = _spot_price(lane, spot["weight_kg"])
                    except ValueError as exc:
                        unpriced = f"price_tariff: {exc}"
                        continue
                    if (total, billed) != (D(spot["total"]), D(spot["billed_kg"])):
                        wrong.append(
                            f"{spot['lane']} {spot['weight_kg']} kg: {total} on {billed} kg, "
                            f"expected {spot['total']} on {spot['billed_kg']} kg"
                        )
                if wrong:
                    outcome, detail = "parsed wrong", "; ".join(wrong)
                elif unpriced:
                    outcome, detail = "not priced", unpriced
                else:
                    outcome, detail = "parsed right", ""
        results.append(Variant(row["file"], row["change"], outcome, detail, outcome in row["safe_outcomes"], gap))
    return results


def _header_row(ws) -> int | None:
    """The first table header: the row with a cell reading Transit."""
    for row in ws.iter_rows():
        if any(str(c.value or "").strip().lower() == "transit" for c in row):
            return row[0].row
    return None


def _column(ws, header: int, name: str) -> int | None:
    for cell in ws[header]:
        if str(cell.value or "").strip().lower() == name:
            return cell.column
    return None


def _swap_transit_minimum(ws, header: int) -> None:
    a, b = _column(ws, header, "transit"), _column(ws, header, "minimum")
    for r in range(header, ws.max_row + 1):
        ws.cell(r, a).value, ws.cell(r, b).value = ws.cell(r, b).value, ws.cell(r, a).value


def _remarks_column(ws, header: int) -> None:
    after = _column(ws, header, "minimum")
    ws.insert_cols(after + 1)
    ws.cell(header, after + 1).value = "Remarks"


def _blank_row_in_table(ws, header: int) -> None:
    ws.insert_rows(header + 3)


def _numbers_as_text(ws, header: int) -> None:
    for row in ws.iter_rows(min_row=header):
        for cell in row:
            if isinstance(cell.value, int | float) and not isinstance(cell.value, bool):
                cell.value = f"{cell.value:.2f}".replace(".", ",")


def _cover_sheet(book) -> None:
    book.create_sheet("Cover", 0)["A1"] = "Air export tariff: rates on the next sheet."


# Layout-only changes: the data stays the same, so a right parse equals the original sheet's parse.
XLSX_CHANGES = {
    "control": ("saved again with merged cells split, as a control", lambda ws, header: None),
    "transit-minimum-swapped": ("Transit and Minimum columns swapped", _swap_transit_minimum),
    "remarks-column": ("an empty Remarks column after Minimum", _remarks_column),
    "blank-row": ("a blank row inside the table", _blank_row_in_table),
    "numbers-as-text": ("every number as text with a decimal comma", _numbers_as_text),
    "cover-sheet": ("a cover sheet before the tariff", None),
}
TARIFF_FIELDS = ("carrier", "origin", "destination", "currency", "valid_from", "valid_to", "transit_days", "minimum")
TARIFF_FIELDS += ("breaks", "fuel_per_kg", "security_per_kg")


def _facts(tariffs: list[LaneTariff]) -> set[tuple]:
    return {tuple(getattr(t, f) for f in TARIFF_FIELDS) for t in tariffs}


def audit_xlsx_changes(folder: Path = START_KIT) -> list[Variant]:
    """Every layout change in XLSX_CHANGES on every spreadsheet in the kit: parsed to the same tariffs as the
    original, or refused. Known gaps are in `tariffs/variants/xlsx_known_gaps.json`, keyed `change:file`."""
    import io

    import openpyxl

    gaps_file = TARIFF_FIXTURES / "variants" / "xlsx_known_gaps.json"
    gaps = json.loads(gaps_file.read_text()) if gaps_file.exists() else {}
    results = []
    for path in sorted(folder.glob("*.xlsx")):
        original = _facts(agent.parse(path.name, path.read_bytes()))
        for key, (change, edit) in XLSX_CHANGES.items():
            book = openpyxl.load_workbook(path)
            ws = book.worksheets[0]
            for merged in list(ws.merged_cells.ranges):
                ws.unmerge_cells(str(merged))
            if edit is None:
                _cover_sheet(book)
            else:
                edit(ws, _header_row(ws))
            buffer = io.BytesIO()
            book.save(buffer)
            label = f"{key}:{path.name}"
            try:
                parsed = agent.parse(path.name, buffer.getvalue())
                findings = agent.check(parsed)
            except ValueError as exc:
                outcome, detail = "refused", f"parse: {exc}"
            else:
                if findings:
                    outcome, detail = "refused", f"check: {findings[0]}"
                elif _facts(parsed) == original:
                    outcome, detail = "parsed right", ""
                else:
                    diff = sorted(_facts(parsed) - original)
                    outcome = "parsed wrong"
                    detail = (
                        f"{len(diff)} lanes differ from the original, e.g. {diff[0][1]}-{diff[0][2]}" if diff else ""
                    )
            safe = outcome in ("parsed right", "refused")
            results.append(Variant(label, change, outcome, detail, safe, gaps.get(label)))
    return results


@dataclass
class Routing:
    case_id: str
    expected: str
    got: str | None  # None: the rules leave it to the model
    rule: str
    ok: bool
    known_gap: str | None = None


def _route(row: dict) -> tuple[MailKind | None, str]:
    """The router's rules on one probe mail, in a fresh database set up as the row's `setup` says."""
    conn = db.connect(":memory:")
    db.init(conn)
    ctx = Context(settings(), conn, None, None, None, None)
    raw = {"id": 10, "to": "pixel@hackloop.mail", "created_at": "2026-11-21T15:00:00Z"} | row["mail"]
    message = to_message(raw, "in")
    address = re.search(r"[\w.+-]+@[\w.-]+", message.sender)
    if row["setup"]["sender_on_carrier_list"]:
        conn.execute(
            "INSERT INTO carriers (name, kind, email, serves) VALUES ('c', 'airline', ?, '')",
            (address.group() if address else message.sender,),
        )
    purpose = row["setup"]["replies_to"]
    if purpose:
        customer = "buyer@acme-gmbh.hackloop.mail"
        first = to_message(
            {"id": 1, "from": customer, "to": "pixel@hackloop.mail", "subject": "Air freight quote", "body": "",
             "created_at": "2026-11-21T14:50:00Z"},
            "in",
        )  # fmt: skip
        first = poller.ingest(ctx, first)
        conn.execute(
            "INSERT INTO sessions (id, customer, state, received_at) VALUES (?, ?, 'quoted', ?)",
            (first.id, customer, first.sent_at.isoformat()),
        )
        to = customer if purpose == "quote" else message.sender
        _store(ctx, first.model_copy(update={"id": 2, "direction": "out", "to": to}), purpose)
        message = message.model_copy(update={"in_reply_to": 2, "references": (1, 2)})
    message = poller.ingest(ctx, message)
    try:
        return router.rules(ctx, message)
    finally:
        conn.close()


def audit_routing() -> list[Routing]:
    """Per probe mail (`mails/routing_probes.jsonl`): the kind the router's rules give. Leaving a mail to the model
    is fine when the model may answer that kind; a wrong kind decided by rules is not."""
    results = []
    model_kinds = {k.value for k in router.MODEL_KINDS}
    for row in _jsonl(FIXTURES / "mails" / "routing_probes.jsonl"):
        kind, rule = _route(row)
        got = kind.value if kind else None
        ok = got == row["expected"] or (got is None and row["expected"] in model_kinds)
        results.append(Routing(row["case_id"], row["expected"], got, rule, ok, row.get("known_gap")))
    return results


@dataclass
class BidRead:
    case_id: str
    got: str  # "total currency transit", or "refused" when the rules leave it to the model
    expected: str
    ok: bool
    known_gap: str | None = None


def audit_bids() -> list[BidRead]:
    """Per probe answer (`mails/bid_probes.jsonl`): what `pricing.bids.parse` reads. Refusing is always safe (the
    model reads it); reading a different total, currency or transit than the reference is not."""
    results = []
    for row in _jsonl(FIXTURES / "mails" / "bid_probes.jsonl"):
        req = row["request"]
        rfq = Rfq(
            session_id=1,
            message_id=1,
            customer="bench",
            origin=req["origin"],
            destination=req["destination"],
            weight_kg=D(req["weight_kg"]),
            volume_cbm=D(req["volume_cbm"]),
            received_at=NOW,
            deadline_at=NOW,
        )
        message = _message("Re: Rate request", row["body"]).model_copy(update={"sender": "carrier@example.com"})
        option = bids.parse(message, rfq)
        exp = row["expected"]
        want = f"{exp['total']} {exp['currency']} {exp['transit_days']}d" if exp else "refused"
        got = f"{option.total} {option.currency} {option.transit_days}d" if option else "refused"
        results.append(BidRead(row["case_id"], got, want, got == want or got == "refused", row.get("known_gap")))
    return results


def _print(title: str, report: Report, ok: str) -> None:
    print(f"\n{title}: {report.passed}/{report.total}")
    for failure in report.failures:
        print(f"    FAIL {failure}")
    if not report.failures:
        print(f"    ok   {ok}")


def open_gaps() -> dict[str, list[str]]:
    """Every known gap the code still shows, grouped by the owner its note names."""
    found = [(r.case_id, r.known_gap) for r in audit_rewrites() if r.by_rules and not r.right and r.known_gap]
    found += [(v.file, v.known_gap) for v in audit_variants() + audit_xlsx_changes() if not v.safe and v.known_gap]
    found += [(r.case_id, r.known_gap) for r in audit_routing() if not r.ok and r.known_gap]
    found += [
        (r.fixture, r.known_gap) for r in audit_live() + audit_live_mutations() if r.right is False and r.known_gap
    ]
    found += [(b.case_id, b.known_gap) for b in audit_bids() if not b.ok and b.known_gap]
    seen_gaps = set()
    for r in document_conflicts.audit():
        key = f"{r.case.change}:{r.case.layout}"
        if r.right is False and r.known_gap and r.known_gap not in seen_gaps:
            seen_gaps.add(r.known_gap)
            found.append((f"document conflict {key}", r.known_gap))
    by_owner: dict[str, list[str]] = {}
    for case, note in found:
        owner = m.group(1) if (m := re.search(r"Owner: (\w+)", note)) else "unassigned"
        by_owner.setdefault(owner, []).append(f"{case}: {note}")
    return by_owner


def print_gaps() -> None:
    for owner, notes in sorted(open_gaps().items()):
        print(f"## {owner} ({len(notes)})\n")
        seen = set()
        for line in notes:
            case, note = line.split(": ", 1)
            if note in seen:  # one note shared by several cases
                print(f"- {case}: same as above")
                continue
            seen.add(note)
            print(f"- {case}: {note}")
        print()


def main() -> int:
    if "--gaps" in sys.argv:
        print_gaps()
        return 0
    if "--stub" in sys.argv:
        print("\n".join(live_stubs()) or "every captured request has its row in rfqs/live.jsonl")
        return 0
    tariffs, errors = load_start_kit()

    print("=" * 70)
    print(" PIXEL generalization benchmark")
    print("=" * 70)

    prices = audit_hand_prices(tariffs)
    _print("[1] Hand-calculated prices", prices, "every total and billed weight matches calc.py")

    sources, lanes = audit_sources(tariffs, errors)
    _print("[2] Tariff sources", sources, f"{lanes} lanes, lane counts match, no check() findings")

    rewrites = audit_rewrites()
    rules = [r for r in rewrites if r.by_rules]
    wrong = [r for r in rules if not r.right and not r.known_gap]
    gaps = [r for r in rules if not r.right and r.known_gap]
    print(f"\n[3] Rewrites: {len(rules)}/{len(rewrites)} by rules, {len(rewrites) - len(rules)} need the model")
    for r in rewrites:
        if not r.by_rules:
            status = "model"
        elif r.right:
            status = "rules"
        else:
            status = "GAP" if r.known_gap else "WRONG"
        print(f"    {status:<5} {r.case_id}" + (f"  ({'; '.join(r.wrong_fields)})" if r.wrong_fields else ""))
        if r.known_gap and r.right:
            print("          now right: remove its known_gap")
    for r in gaps:
        print(f"    gap   {r.case_id}: {r.known_gap}")
    if wrong:
        print(f"    FAIL {len(wrong)} rules extractions disagree with the expected fields and are not known gaps")

    partial, unchecked = audit_partial_cases()
    _print("[4] Partial cases with a rules invariant", partial, "no guesses, urgency heard, no invented weight")
    print(f"    ({unchecked} more cases have no rules invariant yet; see tests/fixtures/rfqs/README.md)")

    variants = audit_variants()
    right = sum(v.outcome == "parsed right" for v in variants)
    refused = sum(v.outcome == "refused" for v in variants)
    unsafe = [v for v in variants if not v.safe and not v.known_gap]
    print(f"\n[5] Changed tariff layouts: {right} parsed right, {refused} refused (model reads), of {len(variants)}")
    for v in variants:
        status = "ok" if v.safe else ("GAP" if v.known_gap else "FAIL")
        print(f"    {status:<4} {v.outcome:<12} {v.change}" + (f"  ({v.detail})" if v.detail else ""))
        if v.known_gap and not v.safe:
            print(f"         gap: {v.known_gap}")
        elif v.known_gap:
            print("         now safe: remove its known_gap")

    routing = audit_routing()
    misrouted = [r for r in routing if not r.ok and not r.known_gap]
    by_rules = sum(r.got is not None for r in routing)
    print(f"\n[6] Routing: {sum(r.ok for r in routing)}/{len(routing)} right, {by_rules} decided by rules")
    for r in routing:
        status = "ok" if r.ok else ("GAP" if r.known_gap else "FAIL")
        got = r.got or "model"
        print(f"    {status:<4} {r.case_id}: {got} ({r.rule})" + ("" if r.ok else f", expected {r.expected}"))
        if r.known_gap and not r.ok:
            print(f"         gap: {r.known_gap}")
        elif r.known_gap:
            print("         now right: remove its known_gap")

    bid_reads = audit_bids()
    misread = [b for b in bid_reads if not b.ok and not b.known_gap]
    read = sum(b.got != "refused" for b in bid_reads)
    print(f"\n[7] Carrier bids: {sum(b.ok for b in bid_reads)}/{len(bid_reads)} safe, {read} read by rules")
    for b in bid_reads:
        status = "ok" if b.ok else ("GAP" if b.known_gap else "FAIL")
        print(f"    {status:<4} {b.case_id}: {b.got}" + ("" if b.got == b.expected else f", reference {b.expected}"))
        if b.known_gap and not b.ok:
            print(f"         gap: {b.known_gap}")
        elif b.known_gap:
            print("         now safe: remove its known_gap")

    sheet_changes = audit_xlsx_changes()
    unsafe_sheets = [v for v in sheet_changes if not v.safe and not v.known_gap]
    right = sum(v.outcome == "parsed right" for v in sheet_changes)
    refused = sum(v.outcome == "refused" for v in sheet_changes)
    print(f"\n[8] Spreadsheet layout changes: {right} parsed the same, {refused} refused, of {len(sheet_changes)}")
    for v in sheet_changes:
        if not v.safe or v.outcome == "refused":
            status = "ok" if v.safe else ("GAP" if v.known_gap else "FAIL")
            print(f"    {status:<4} {v.outcome:<12} {v.file}" + (f"  ({v.detail})" if v.detail else ""))
    if not unsafe_sheets:
        print("    ok   every other change parsed to the original's tariffs")

    live = audit_live()
    live_wrong = [r for r in live if r.right is False and not r.known_gap]
    read = sum(r.by_rules for r in live)
    print(f"\n[9] Captured requests: {sum(bool(r.right) for r in live)}/{len(live)} read right, {read} by rules, "
          f"{sum(bool(r.todo) for r in live)} rows to write")  # fmt: skip
    for k, (right, total) in live_by_field(live).items():
        print(f"    {k:<16} {right}/{total}")
    for r in live:
        if r.right is False:
            status = "GAP" if r.known_gap else "FAIL"
            print(f"    {status:<4} {r.fixture}: {'; '.join(r.wrong_fields)}")
        elif r.right and r.known_gap:
            print(f"    ok   {r.fixture}: now right, remove its known_gap")
        elif r.todo:
            print(f"    todo {r.fixture}: {r.todo}")
        elif r.right is None:
            print(f"    model {r.fixture}")

    mutated = audit_live_mutations()
    mutated_wrong = [r for r in mutated if r.right is False and not r.known_gap]
    print(f"\n[10] Captured requests, changed: {sum(bool(r.right) for r in mutated)}/{len(mutated)} read right, "
          f"{sum(r.right is None for r in mutated)} left to the model")  # fmt: skip
    for r in mutated:
        if r.right is False:
            status = "GAP" if r.known_gap else "FAIL"
            print(f"    {status:<4} {r.fixture}: {'; '.join(r.wrong_fields)}")
        elif r.right and r.known_gap:
            print(f"    ok   {r.fixture}: now right, remove it from known_gap_mutations")
    if not mutated_wrong:
        print("    ok   an old quoted request, a reply's subject and pounds change no field")

    conflicts = document_conflicts.audit()
    conflicts_wrong = [r for r in conflicts if r.right is False and not r.known_gap]
    print(f"\n[11] Mail against its document: {sum(bool(r.right) for r in conflicts)}/{len(conflicts)} read the "
          f"document's figures, {sum(r.right is None for r in conflicts)} left to the model")  # fmt: skip
    layouts = document_conflicts.LAYOUTS
    print(f"    {'mail states wrong':<18} " + " ".join(f"{name:>16}" for name in layouts))
    for change, row in document_conflicts.by_change(conflicts).items():
        cells = []
        for name in layouts:
            ok, bad, model = row.get(name, (0, 0, 0))
            cells.append(f"{ok}/{ok + bad + model}" + ("" if not model else f" m{model}"))
        print(f"    {change:<18} " + " ".join(f"{c:>16}" for c in cells))
    noted = set()
    for r in conflicts:
        if r.right is False and r.known_gap and r.known_gap not in noted:
            noted.add(r.known_gap)
            print(f"    gap  {r.case.change}: {r.known_gap}")
    stale = {f"{r.case.change}:{r.case.layout}" for r in conflicts if r.right and r.known_gap}
    for key in sorted(stale):
        print(f"    ok   {key}: now right, narrow or remove its known_gap")
    for r in conflicts_wrong[:20]:
        print(f"    FAIL {r.name}: {'; '.join(r.wrong_fields)}")

    failed = bool(
        prices.failures
        or sources.failures
        or wrong
        or partial.failures
        or unsafe
        or misrouted
        or misread
        or unsafe_sheets
        or live_wrong
        or mutated_wrong
        or conflicts_wrong
    )
    print("\n" + "=" * 70)
    print(" FAILED" if failed else " PASSED")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
