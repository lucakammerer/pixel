"""KPIs of this checkout on replayed requests, and what changed against another checkout.

uv run python -m tools.kpi > base.json          # every request among the mail fixtures, as JSON
uv run python -m tools.kpi --base base.json     # the same as Markdown, compared with that result

Every mail fixture is replayed offline (`tools/replay.py` `run`) on a database that holds the start kit; it counts
as a request when the router places it as one. Offline means sheet prices only and every model call fails: a
request that needs the model is missed, a model call counts as attempted, response time is not measured.
`.github/workflows/ci.yml` posts the comparison with the base branch into every pull request.
"""

import argparse
import asyncio
import dataclasses
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from pixel.agents.tariffs.agent import ingest_folder
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Event, MailKind
from tools import replay

ROOT = Path(__file__).parent.parent


async def _kit() -> str:
    path = str(Path(tempfile.mkdtemp()) / "kit.sqlite3")
    ctx = build_context(dataclasses.replace(settings(), dry_run=True, db_path=path))
    ctx.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(replay._offline))
    await ingest_folder(ctx, ROOT / "data" / "start-kit")
    ctx.db.close()
    return path


def _row(request: str, evs: list[Event]) -> dict | None:
    if not any(e.kind == "route" and e.payload.get("kind") == MailKind.CUSTOMER_RFQ.value for e in evs):
        return None
    drafted = [e.payload["option"] for e in evs if e.kind == "quote.drafted"]
    answered = bool(drafted) and not any(e.kind == "quote.missed" for e in evs)
    option = drafted[-1] if answered else {}
    return {
        "request": request, "answered": answered, "total": option.get("total"), "currency": option.get("currency"),
        "eur": option.get("total_eur"), "carrier": option.get("carrier"),
        "model_calls": sum(e.kind == "llm.call" for e in evs),
    }  # fmt: skip


async def measure(fixtures: Path) -> list[dict]:
    kit = await _kit()
    rows = []
    for path in sorted(fixtures.glob("*.json")):
        row = _row(path.stem, await replay.run([json.loads(path.read_text())], online=False, db=kit))
        if row:
            rows.append(row)
    return rows


def _cell(row: dict | None) -> str:
    if row is None:
        return "—"
    if not row["answered"]:
        return f"missed · {row['model_calls']} model calls"
    return f"{row['total']} {row['currency']} · {row['carrier']} · {row['model_calls']} model calls"


def _price(was: dict | None, new: dict) -> str:
    if not (was and was["eur"] and new["eur"]) or was["eur"] == new["eur"]:
        return ""
    return f"{(Decimal(new['eur']) / Decimal(was['eur']) - 1) * 100:+.1f}%"


def report(rows: list[dict], base: list[dict] | None) -> str:
    """Markdown for the pull request: the totals of both sides, then every request whose answer differs."""
    sides = ([("Base", base)] if base is not None else []) + [("This branch", rows)]
    old = {r["request"]: r for r in base or ()}
    changed = [(old.get(r["request"]), r) for r in rows if old.get(r["request"]) != r]
    out = [
        f"### KPIs on {len(rows)} replayed requests",
        "",
        "| | " + " | ".join(name for name, _ in sides) + " |",
        "| --- |" + " --- |" * len(sides),
        "| Answered | " + " | ".join(f"{sum(r['answered'] for r in side)} of {len(side)}" for _, side in sides) + " |",
        "| Model calls | " + " | ".join(str(sum(r["model_calls"] for r in side)) for _, side in sides) + " |",
        "",
    ]
    if changed:
        out += ["| Request | Base | This branch | Price |", "| --- | --- | --- | --- |"]
        out += [f"| {new['request']} | {_cell(was)} | {_cell(new)} | {_price(was, new)} |" for was, new in changed]
    else:
        out.append("No request changed its answer, price, carrier or model calls.")
    out += ["", "Offline replay on the start kit: sheet prices only, model calls fail, response time is not measured."]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixtures", nargs="?", type=Path, default=ROOT / "tests" / "fixtures" / "mails")
    parser.add_argument("--base", type=Path, help="another checkout's JSON; a missing file compares with nothing")
    args = parser.parse_args()
    rows = asyncio.run(measure(args.fixtures))
    if args.base is None:
        print(json.dumps(rows, indent=2))
    else:
        print(report(rows, json.loads(args.base.read_text()) if args.base.exists() else None))


if __name__ == "__main__":
    main()
