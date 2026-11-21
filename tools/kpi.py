"""Accuracy, cost and speed of this checkout on replayed requests, and what changed against another checkout.

uv run python -m tools.kpi > branch.json                          # the request fixtures, offline, as JSON
uv run python -m tools.kpi --online --db data/pixel.sqlite3       # the requests stored in that database, online
uv run python -m tools.kpi --report branch.json --base base.json  # both results as Markdown for the pull request

A request is replayed with `tools/replay.py` `run`: a mail fixture on a database that holds the start kit, or with
`--db` a stored request and the carriers' answers to it on a copy of that database. The customer's later mails are
left out, because an acceptance books at the portal. Its price is the one code picks from every option the mails
left behind (`agents/rfq/agent.py` `fallback`).

Offline there are sheet prices only and every model call fails: a request that needs the model is missed, cost and
speed are not measured. `--online` reads the real portal and spends through the real gateway (R28), mail stays dry.
`.github/workflows/ci.yml` posts the offline comparison into every pull request, `tools/kpi_pr.sh` the online one.
"""

import argparse
import asyncio
import dataclasses
import json
import sqlite3
import statistics
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from pixel.agents.rfq import agent as rfq
from pixel.agents.tariffs.agent import ingest_folder
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Event, MailKind
from pixel.core.context import Context
from tools import replay

ROOT = Path(__file__).parent.parent
QUOTE_MAILS = (MailKind.CUSTOMER_RFQ.value, MailKind.CARRIER_BID.value, MailKind.RATEDESK_REPLY.value)
COST_POINTS, COST_ZERO_USD = 40, Decimal("0.10")  # R28
TIME_POINTS, TIME_FULL_S, TIME_ZERO_S = 15, 120, 360  # R29


async def _kit() -> str:
    path = str(Path(tempfile.mkdtemp()) / "kit.sqlite3")
    ctx = build_context(dataclasses.replace(settings(), dry_run=True, db_path=path))
    ctx.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(replay._offline))
    await ingest_folder(ctx, ROOT / "data" / "start-kit")
    ctx.db.close()
    return path


def _stored(db: str) -> dict[str, list[dict]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    marks = ",".join("?" * len(QUOTE_MAILS))
    rows = conn.execute(
        f"SELECT session_id, data FROM messages WHERE direction = 'in' AND kind IN ({marks}) AND session_id IN"
        " (SELECT id FROM messages WHERE direction = 'in' AND kind = ?) ORDER BY id",
        (*QUOTE_MAILS, MailKind.CUSTOMER_RFQ.value),
    ).fetchall()
    conn.close()
    requests: dict[str, list[dict]] = {}
    for session_id, data in rows:
        requests.setdefault(f"session {session_id}", []).append(json.loads(data)["raw"])
    return requests


async def _row(ctx: Context, evs: list[Event], request: str) -> dict | None:
    routed = next((e for e in evs if e.kind == "route" and e.payload.get("kind") == MailKind.CUSTOMER_RFQ.value), None)
    if routed is None:
        return None
    quoted_at = next((e.ts for e in evs if e.kind == "quote.drafted"), None)
    quote = await rfq.fallback(ctx, routed.session_id)
    option = quote.option if quote else None
    return {
        "request": request, "answered": quote is not None,
        "total": option and str(option.total), "currency": option and option.currency,
        "eur": option and str(option.total_eur), "carrier": option and option.carrier,
        "model_calls": sum(e.kind == "llm.call" for e in evs),
        "usd": str(sum((e.cost_usd for e in evs), Decimal(0))),
        "seconds": quoted_at and round((quoted_at - evs[0].ts).total_seconds(), 1),
    }  # fmt: skip


async def measure(fixtures: Path, db: str | None, online: bool) -> dict:
    requests = _stored(db) if db else {p.stem: [json.loads(p.read_text())] for p in sorted(fixtures.glob("*.json"))}
    source = db or await _kit()
    rows = []
    for request, raws in requests.items():
        row = await _row(*await replay.run(raws, online, source), request)
        if row:
            rows.append(row)
    return {"online": online, "requests": rows}


def _accuracy(rows: list[dict]) -> tuple[str, str]:
    answered = [r for r in rows if r["answered"]]
    return f"{len(answered)} of {len(rows)}", f"{sum(Decimal(r['eur']) for r in answered)} EUR"


def _cost(rows: list[dict]) -> str:
    """R28: the gateway spend divided by the quotes it bought; full points at no spend, none at COST_ZERO_USD."""
    answered = sum(r["answered"] for r in rows)
    if not answered:
        return "—"
    usd = sum(Decimal(r["usd"]) for r in rows) / answered
    return f"{usd:.5f} USD, {max(Decimal(0), COST_POINTS * (1 - usd / COST_ZERO_USD)):.1f} of {COST_POINTS} points"


def _speed(rows: list[dict]) -> str:
    """R29: full points up to TIME_FULL_S, none at TIME_ZERO_S."""
    seconds = [r["seconds"] for r in rows if r["answered"] and r["seconds"] is not None]
    if not seconds:
        return "—"
    points = [TIME_POINTS * min(1, max(0, (TIME_ZERO_S - s) / (TIME_ZERO_S - TIME_FULL_S))) for s in seconds]
    return f"{statistics.median(seconds):.0f} s, {statistics.mean(points):.1f} of {TIME_POINTS} points"


def _cell(row: dict | None, online: bool) -> str:
    if row is None:
        return "—"
    spent = f"{Decimal(row['usd']):.5f} USD" if online else f"{row['model_calls']} model calls"
    if not row["answered"]:
        return f"missed · {spent}"
    took = f" · {row['seconds']} s" if online else ""
    return f"{row['total']} {row['currency']} · {row['carrier']} · {spent}{took}"


def _price(was: dict | None, new: dict | None) -> str:
    if not (was and new and was["eur"] and new["eur"]) or was["eur"] == new["eur"]:
        return ""
    return f"{(Decimal(new['eur']) / Decimal(was['eur']) - 1) * 100:+.1f}%"


def report(branch: dict, base: dict | None) -> str:
    """Markdown for the pull request: the three KPIs of both sides, then every request whose answer differs."""
    online = branch["online"]
    sides = ([("Base", base["requests"])] if base else []) + [("This branch", branch["requests"])]
    lines = {
        "Accuracy: answered": [_accuracy(rows)[0] for _, rows in sides],
        "Accuracy: sum of the quoted prices": [_accuracy(rows)[1] for _, rows in sides],
        "Cost: spend per quote": [
            _cost(rows) if online else f"{sum(r['model_calls'] for r in rows)} model calls, not charged offline"
            for _, rows in sides
        ],
        "Speed: median time to the quote": [_speed(rows) if online else "not measured offline" for _, rows in sides],
    }
    old, new = ({r["request"]: r for r in side["requests"]} for side in (base or {"requests": ()}, branch))

    def differs(name: str) -> bool:
        # A request the router no longer places as one is missing on the branch: it has to show up as a change.
        return (
            name not in old
            or name not in new
            or any(old[name][k] != new[name][k] for k in ("answered", "eur", "carrier", "model_calls"))
        )

    changed = [name for name in sorted(old.keys() | new.keys()) if differs(name)]
    mode = "online: real portal and gateway, mail dry" if online else "offline: start kit, no model"
    out = [
        f"### KPIs on {len(new)} replayed requests ({mode})",
        "",
        "| | " + " | ".join(name for name, _ in sides) + " |",
        "| --- |" + " --- |" * len(sides),
        *("| " + " | ".join([label, *values]) + " |" for label, values in lines.items()),
        "",
    ]
    if changed:
        out += ["| Request | Base | This branch | Price |", "| --- | --- | --- | --- |"]
        out += [
            f"| {name} | {_cell(old.get(name), online)} | {_cell(new.get(name), online)}"
            f" | {_price(old.get(name), new.get(name))} |"
            for name in changed
        ]
    else:
        out.append("No request changed its answer, price, carrier or model calls.")
    out += ["", "Accuracy has no points here: the best price is the scorer's."]
    if online:
        out[-1] += (
            " Speed is our own time to the quote: no carrier answers a replay, so a door-to-port request waits"
            " for its truckers until the target."
        )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixtures", nargs="?", type=Path, default=ROOT / "tests" / "fixtures" / "mails")
    parser.add_argument("--db", help="replay the requests stored in this database, on a copy of it")
    parser.add_argument("--online", action="store_true", help="real portal and gateway; mail stays dry")
    parser.add_argument("--report", type=Path, help="a result as JSON: print it as Markdown instead of measuring")
    parser.add_argument("--base", type=Path, help="with --report: the result to compare with; may be missing or empty")
    args = parser.parse_args()
    if args.report:
        has_base = args.base and args.base.exists() and args.base.stat().st_size
        print(report(json.loads(args.report.read_text()), json.loads(args.base.read_text()) if has_base else None))
    else:
        print(json.dumps(asyncio.run(measure(args.fixtures, args.db, args.online)), indent=2))


if __name__ == "__main__":
    main()
