"""The pickup memory against the truckers' real answers, offline on a database file.

uv run python -m tools.memory_proof <database>

Every door-to-port request whose truckers answered with a price and a pickup time (two or more) is priced again by
`pickup.estimates` from the answers before it (a copy of the database cut off at the session's first event), and
the balanced trucker of the estimates (`selection.balanced`) is set against the balanced trucker of the real answers.
Prints as JSON how many requests the memory could price, how often it picks the same trucker, and the difference as
a share of the request's all-in quote: what an estimate instead of the answers would have moved the total by. The
file itself is never written.
"""

import argparse
import json
import sqlite3
import statistics
import tempfile
from decimal import Decimal
from pathlib import Path

from pixel.config import settings
from pixel.contracts import RateOption, Rfq
from pixel.core import db
from pixel.core.context import Context
from pixel.pricing import pickup, selection, session

SHARES = ("1", "2.5", "5")


def _balanced(options: list[RateOption]) -> RateOption | None:
    timed = [o for o in options if o.pickup_hours is not None]
    return selection.balanced(timed, lambda o: o.pickup_hours, selection.DEFAULT_RULE) if len(timed) > 1 else None


def measure(source: str, worst: int = 8) -> dict:
    work = str(Path(tempfile.mkdtemp()) / "memory.sqlite3")
    original, copy = sqlite3.connect(f"file:{source}?mode=ro", uri=True), sqlite3.connect(work)
    original.backup(copy)
    copy.close()
    ctx = Context(settings(), db.connect(work), None, None, None, None)
    sessions = [r[0] for r in ctx.db.execute(
        "SELECT DISTINCT session_id FROM events WHERE kind = 'quote.drafted' ORDER BY session_id DESC")]  # fmt: skip
    cases, unpriced = [], 0
    for session_id in sessions:  # newest first: each cut leaves the answers given before the request
        read = ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'rfq.extracted' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        quoted = original.execute(
            "SELECT json_extract(payload, '$.option.total_eur') FROM events WHERE session_id = ?"
            " AND kind = 'quote.drafted' ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        answers = [session.option(json.loads(p)["option"]) for (p,) in ctx.db.execute(
            "SELECT payload FROM events WHERE session_id = ? AND kind = 'option.found'"
            " AND json_extract(payload, '$.option.trucker') IS NOT NULL ORDER BY id", (session_id,))]  # fmt: skip
        real = _balanced(list({a.trucker: a for a in answers}.values()))
        first = ctx.db.execute("SELECT MIN(id) FROM events WHERE session_id = ?", (session_id,)).fetchone()[0]
        ctx.db.execute("DELETE FROM events WHERE id >= ?", (first,))
        rfq = json.loads(read[0]) if read else {}
        if real is None or not quoted or not (rfq.get("door_pickup") and rfq.get("pickup_zone")):
            continue
        estimated = _balanced(pickup.estimates(ctx, Rfq.model_validate({k: v for k, v in rfq.items()
                                                                        if k in Rfq.model_fields}), []))  # fmt: skip
        if estimated is None:
            unpriced += 1
            continue
        off = estimated.total_eur - real.total_eur
        cases.append({"session": session_id, "zone": rfq["pickup_zone"], "real": f"{real.trucker} {real.total_eur}",
                      "memory": f"{estimated.trucker} {estimated.total_eur}", "same": real.trucker == estimated.trucker,
                      "off_eur": str(off), "share_pct": round(float(abs(off) / Decimal(quoted[0]) * 100), 2)})  # fmt: skip
    original.close()
    ctx.db.close()
    return {
        "requests": len(cases) + unpriced,
        "priced": len(cases),
        "same_trucker": sum(c["same"] for c in cases),
        "median_off_eur": str(statistics.median(abs(Decimal(c["off_eur"])) for c in cases)) if cases else None,
        "over_share_pct": {s: sum(c["share_pct"] > float(s) for c in cases) for s in SHARES},
        "worst": sorted(cases, key=lambda c: -c["share_pct"])[:worst],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database")
    parser.add_argument("--worst", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(measure(args.database, args.worst), indent=1))


if __name__ == "__main__":
    main()
