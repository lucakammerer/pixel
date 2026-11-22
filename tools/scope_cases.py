"""Axis S: every scope case offline through router, scope decision and pricing, against the answer expected by hand.

uv run python -m tools.scope_cases            # the table as Markdown for bench/NEXT_CASES.md "Axis S: scope"

Each mail in `tests/fixtures/next-cases/scope/` is replayed as live on a database that holds the start kit, no
gateway, no portal. Its expected kind of answer is in `tests/fixtures/next-cases/scope.json`: `quote`, or `no_price`
(the plain answer without a price, or none for a mail that asks for nothing). Where the rules gave up and the model
would have read the request, today is `model`: live it depends on the model's reading. Exit 1 on a wrong case that
is not a known gap.
"""

import asyncio
import json
import sys
from pathlib import Path

from tools import kpi, replay

ROOT = Path(__file__).parent.parent
CASES = ROOT / "tests" / "fixtures" / "next-cases" / "scope"
EXPECTED = CASES.parent / "scope.json"
MODEL = ("rfq.extract", "route")


def outcome(evs, customer: str) -> tuple[str, float | None, bool]:
    """What the customer got (quote, no_price, none), after how many seconds, and whether a model had to read it."""
    start = evs[0].ts if evs else None
    replies = [e for e in evs if e.kind == "mail.out" and e.payload.get("to") == customer]
    quoted = any(e.kind == "quote.sent" for e in evs)
    seconds = round((replies[0].ts - start).total_seconds(), 1) if replies else None
    model = any(e.kind == "llm.call" and e.payload.get("purpose") in MODEL for e in evs)
    return ("quote" if quoted else "no_price" if replies else "none"), seconds, model


def verdict(expected: str, got: str, seconds: float | None, model: bool) -> str:
    right = got == expected or (expected == "no_price" and got == "none")
    if model and not (got != "quote" and seconds is not None and seconds < 5):  # a scope rule declined at once
        return "model"
    return "right" if right else "wrong"


async def measure() -> list[dict]:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    db = await kpi._kit()
    rows = []
    for name, want in expected.items():
        mail = json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))
        evs = await replay.run(replay.raws_of(mail), False, db, as_live=True)
        got, seconds, model = outcome(evs, mail["from"])
        rows.append(
            {
                "case": name,
                **want,
                "today": got,
                "seconds": seconds,
                "verdict": verdict(want["expected"], got, seconds, model),
            }
        )
    return rows


def table(rows: list[dict]) -> str:
    lines = [
        "| Case | Branch | Expected (how sure) | Today | Verdict | Known gap |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        today = r["today"] + (
            f" after {r['seconds']:g} s" if r["seconds"] is not None and r["today"] != "quote" else ""
        )
        lines.append(
            f"| `{r['case']}` | {r['branch']} | {r['expected']} ({r['sure']}) | {today} | {r['verdict']} | {r.get('known_gap', '')} |"
        )
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ("right", "wrong", "model")}
    lines.append(f"\n{len(rows)} cases: {counts['right']} right, {counts['wrong']} wrong, {counts['model']} model.")
    return "\n".join(lines)


def main() -> None:
    rows = asyncio.run(measure())
    print(table(rows))
    sys.exit(1 if any(r["verdict"] == "wrong" and not r.get("known_gap") for r in rows) else 0)


if __name__ == "__main__":
    main()
