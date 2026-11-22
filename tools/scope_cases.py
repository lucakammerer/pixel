"""Axis S: every scope case offline through router, scope decision and pricing, against the answer expected by hand.

uv run python -m tools.scope_cases            # the table as Markdown for bench/NEXT_CASES.md "Axis S: scope"

Each mail in `tests/fixtures/next-cases/scope/` is replayed as live on a database that holds the start kit, no portal.
Its expected kind of answer is in `tests/fixtures/next-cases/scope.json`: `quote`, or `no_price` (the plain answer
without a price, or none for a mail that asks for nothing). Where the rules give up, the model reads the mail live;
here its plausible answers are stubbed, never asked: the reading as an air request written by hand in the case
(`air`: the nearest airports, as round 177's model did) and a reading that names no airport (`blank`). A case is
right only when every plausible answer leads to the right kind; `split` when it depends on the answer. Exit 1 on a
wrong or split case that is not a known gap.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from pixel.agents.rfq.extract import SYSTEM as EXTRACT
from pixel.agents.router.agent import SYSTEM as ROUTE
from tools import kpi, replay

ROOT = Path(__file__).parent.parent
CASES = ROOT / "tests" / "fixtures" / "next-cases" / "scope"
EXPECTED = CASES.parent / "scope.json"
LATE_S = 60  # a scope rule declines at once; the fallback answers without a price at `hard_at`
BLANK = {"origin": None, "destination": None, "weight_kg": None, "volume_cbm": None}


def stub(reading: dict | None, route: str):
    """The gateway answering the router and the extraction with one plausible reading; every other call fails."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/gateway/chat/completions" or reading is None:
            return httpx.Response(503, json={"detail": "replay is offline"})
        system = json.loads(request.content)["messages"][0]["content"]
        if system == ROUTE:
            data = {"kind": route}
        elif system == EXTRACT:
            data = {"pieces": None, "lots": [], "preference": "cheapest", "door_pickup": False,
                    "pickup_postcode": None, "currency": None, "language": "en", "first_name": None, **reading}  # fmt: skip
        else:
            return httpx.Response(503, json={"detail": "replay is offline"})
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}], "usage": usage})

    return answer


def outcome(evs, customer: str) -> str:
    """What the customer got, in order: question, quote, no_price (at once, or late: at `hard_at`, after the rules
    found nothing to decline and no price came), none."""
    start = evs[0].ts if evs else None
    got = []
    for e in evs:
        if e.kind == "mail.out" and e.payload.get("to") == customer:
            purpose = e.payload.get("purpose") or "other"
            if purpose == "no_price":
                purpose += " late" if (e.ts - start).total_seconds() > LATE_S else ""
            got.append(purpose)
    return " → ".join(got) or "none"


def verdict(expected: str, got: str) -> str:
    """One question for a missing fact may come before a quote, never before the plain answer to what we do not do."""
    last = got.split(" → ")[-1]
    if " → " in expected:  # a later mail in the session: each answer in turn
        return "right" if got == expected else "wrong"
    if expected == "quote":
        return "right" if last == "quote" else "wrong"
    return "right" if got in ("no_price", "none") else "late" if got == "no_price late" else "wrong"


async def run(db: str, mail: dict, reading: dict | None, route: str) -> tuple[str, set[str]]:
    raws = replay.raws_of(mail)
    evs = await replay.run(raws, False, db, as_live=True, portal=stub(reading, route))
    asked = {e.payload.get("purpose") for e in evs if e.kind == "llm.call"} & {"route", "rfq.extract"}
    return outcome(evs, raws[0]["from"]), asked


async def measure() -> list[dict]:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    db = await kpi._kit()
    rows = []
    for name, want in expected.items():
        mail = json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))
        got, asked = await run(db, mail, None, "")
        today = {"offline": got}
        if asked:  # the model reads it live: each plausible answer instead of none
            readings = {"air": want.get("air", BLANK), "blank": BLANK}
            today = {k: (await run(db, mail, r, "customer_rfq"))[0] for k, r in readings.items()}
            if "route" in asked:
                today["routed other"] = (await run(db, mail, BLANK, "other"))[0]
        verdicts = {verdict(want["expected"], g) for g in today.values()}
        v = verdicts.pop() if len(verdicts) == 1 else "split" if "right" in verdicts or "late" in verdicts else "wrong"
        rows.append({"case": name, **want, "today": today, "verdict": v})
    return rows


def table(rows: list[dict]) -> str:
    lines = [
        "| Case | Branch | Step from | Expected (how sure) | Today | Verdict | Known gap |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        t = r["today"]
        today = t["offline"] if "offline" in t else "; ".join(f"{k}: {g}" for k, g in t.items())
        lines.append(f"| `{r['case']}` | {r['branch']} | {r.get('step', '')} | {r['expected']} ({r['sure']}) | {today} | {r['verdict']} | {r.get('known_gap', '')} |")  # fmt: skip
    counts = ", ".join(f"{sum(r['verdict'] == v for r in rows)} {v}" for v in ("right", "late", "split", "wrong"))
    lines.append(f"\n{len(rows)} cases: {counts}.")
    return "\n".join(lines)


def main() -> None:
    rows = asyncio.run(measure())
    print(table(rows))
    sys.exit(1 if any(r["verdict"] in ("wrong", "split") and not r.get("known_gap") for r in rows) else 0)


if __name__ == "__main__":
    main()
