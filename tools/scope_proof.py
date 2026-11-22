"""The scope decision of this checkout over every live request in a database: not one quote the scorer accepted may become a decline.

uv run python -m tools.scope_proof <database>      # read-only, on copies; exit 1 when an accepted quote becomes a decline

Each stored customer request (its first mail, as the API gave it) is replayed offline through today's router, rules
and scope decision on a copy of that database, beside the scorer's verdict (`feedback` events). Declined means the
plain answer without a price went out before any price was sought. A request only the model read is decided by the
rules alone here (offline there is no model), and attachments are read without their bytes (not stored).
"""

import asyncio
import json
import sqlite3
import sys
from collections import Counter

from tools import replay
from tools.scope_cases import outcome


async def prove(db: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    verdicts = {
        s: json.loads(p) for s, p in conn.execute("SELECT session_id, payload FROM events WHERE kind = 'feedback'")
    }
    quoted = {s for (s,) in conn.execute("SELECT DISTINCT session_id FROM events WHERE kind = 'quote.sent'")}
    requests = conn.execute(
        "SELECT id, subject, data FROM messages WHERE direction = 'in' AND kind = 'customer_rfq' AND id = session_id"
    ).fetchall()
    conn.close()
    counts, declined = Counter(), []
    for sid, subject, data in requests:
        raw = json.loads(data)
        evs = await replay.run([raw], False, db, as_live=True)
        got = outcome(evs, raw.get("from") or raw["sender"])
        verdict = verdicts.get(sid, {})
        decision = "declined" if got in ("no_price", "none") else "served"
        live = "quoted" if sid in quoted else "not quoted"
        counts[(f"{verdict.get('verdict', 'unscored')}, live {live}", decision)] += 1
        if decision == "declined":
            declined.append(f"round {verdict.get('round')} session {sid} {verdict.get('verdict', 'unscored')} "
                            f"{verdict.get('reason') or ''} {got} {subject!r}")  # fmt: skip
    for (verdict, decision), n in sorted(counts.items()):
        print(f"{verdict}: {n} {decision}")
    print("\n".join(declined))
    return 1 if counts[("accepted, live quoted", "declined")] else 0


def main() -> None:
    sys.exit(asyncio.run(prove(sys.argv[1])))


if __name__ == "__main__":
    main()
