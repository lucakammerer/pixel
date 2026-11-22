"""The scope decision of this checkout over every live request in a database: not one quote the scorer accepted may become a decline.

uv run python -m tools.scope_proof <database>      # read-only, on copies; exit 1 when an accepted quote becomes a decline

Each stored customer request (its first mail, as the API gave it) is replayed offline through today's router, rules
and scope decision on a copy of that database, beside the scorer's verdict (`feedback` events). A scope decline is
the reply that says we only offer air export, even if a question went out first. A missing rate or reply is counted
separately. Recorded model answers can replay for the same prompt. Other prompts and attachment bytes are unavailable.
"""

import asyncio
import json
import sqlite3
import sys
from collections import Counter

from pixel.agents.rfq.compose import OUT_OF_SCOPE
from pixel.contracts import Event
from tools import replay
from tools.scope_cases import outcome


def scope_outcome(evs: list[Event], customer: str, got: str) -> str:
    for event in evs:
        if event.kind != "mail.out" or event.payload.get("to") != customer:
            continue
        if event.payload.get("purpose") == "no_price" and any(
            text in event.payload.get("body", "") for text in OUT_OF_SCOPE.values()
        ):
            return "scope decline"
    match got.split(" → ")[-1]:
        case "quote":
            return "quote"
        case "none":
            return "no reply"
        case _:
            return "unpriced"


async def prove(db: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    verdicts = {
        s: json.loads(p)
        for s, p in conn.execute("SELECT session_id, payload FROM events WHERE kind = 'feedback' ORDER BY id")
    }
    quoted = {s for (s,) in conn.execute("SELECT DISTINCT session_id FROM events WHERE kind = 'quote.sent'")}
    requests = conn.execute(
        "SELECT id, subject, data FROM messages WHERE direction = 'in' AND kind = 'customer_rfq' AND id = session_id"
    ).fetchall()
    cases = [(sid, subject, json.loads(data), replay.answers(conn, sid)) for sid, subject, data in requests]
    conn.close()
    counts, stopped = Counter(), []
    for sid, subject, raw, answers in cases:
        evs = await replay.run([raw], False, db, as_live=True, portal={"answers": answers})
        customer = raw.get("from") or raw["sender"]
        got = outcome(evs, customer)
        verdict = verdicts.get(sid, {})
        decision = scope_outcome(evs, customer, got)
        live = "quoted" if sid in quoted else "not quoted"
        counts[(f"{verdict.get('verdict', 'unscored')}, live {live}", decision)] += 1
        if decision in ("scope decline", "no reply"):
            stopped.append(f"round {verdict.get('round')} session {sid} {verdict.get('verdict', 'unscored')} "
                           f"{verdict.get('reason') or ''} {decision} ({got}) {subject!r}")  # fmt: skip
    for (verdict, decision), n in sorted(counts.items()):
        print(f"{verdict}: {n} {decision}")
    print("\n".join(stopped))
    return int(any(counts[("accepted, live quoted", decision)] for decision in ("scope decline", "no reply")))


def main() -> None:
    sys.exit(asyncio.run(prove(sys.argv[1])))


if __name__ == "__main__":
    main()
