"""The rate card against the rates the portal gave our requests, offline on a database file.

uv run python -m tools.card_proof <database>

Every rate the rate API priced a request with (`option.found`, source portal, with a rate id) is priced again from
the card as it stood before that read: the same lane, airline and billed weight, by `pricing/rate_card.py` `option`
on a copy of the database cut off at that event. The file itself is never written. Prints as JSON how many of these
rates the card could price, the share of those within 0.1 %, 1 % and 5 %, and the worst cases with their cause:
`same`, the card read the bracket in the rate's own dataset version; `scaled`, it read the bracket in another version
and scaled it by the version's factor; `new version`, the rate was the first read of a version the card had not
seen, which no card can know.
"""

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pixel.config import settings
from pixel.contracts import Rfq
from pixel.core import db
from pixel.core.context import Context
from pixel.pricing import calc, rate_card

LIVE = (
    "SELECT id, ts, payload FROM events WHERE kind='option.found' AND json_extract(payload,'$.option.source')='portal'"
    " AND json_extract(payload,'$.option.rate_id') IS NOT NULL AND json_extract(payload,'$.option.trucker') IS NULL"
    " AND json_extract(payload,'$.option.carrier_code') IS NOT NULL ORDER BY id DESC"
)
WITHIN = ("0.1", "1", "5")


def measure(source: str, worst: int = 10) -> dict:
    work = str(Path(tempfile.mkdtemp()) / "card.sqlite3")
    original, copy = sqlite3.connect(f"file:{source}?mode=ro", uri=True), sqlite3.connect(work)
    original.backup(copy)
    original.close()
    copy.close()
    ctx = Context(settings(), db.connect(work), None, None, None, None)
    rows = ctx.db.execute(LIVE).fetchall()
    cases = []
    bill_higher, calc.BILL_HIGHER_BREAK = calc.BILL_HIGHER_BREAK, False  # the card at the weight the rate billed
    try:
        for row in rows:  # newest first: each cut leaves the reads made before the rate
            live = json.loads(row["payload"])["option"]
            ctx.db.execute("DELETE FROM events WHERE id >= ?", (row["id"],))
            at = datetime.fromisoformat(row["ts"])
            request = Rfq(session_id=0, message_id=0, customer="", origin=live["origin"], destination=live["destination"],
                          weight_kg=Decimal(live["billed_kg"]), received_at=at, deadline_at=at)  # fmt: skip
            card = rate_card.option(ctx, request, live["carrier_code"])
            if card is None:
                continue
            current, rates = rate_card._rates(ctx)
            bracket = rate_card._bracket(rate_card.breaks(ctx, live["carrier_code"]), request.weight_kg)
            read_in = rates[(live["origin"], live["destination"], live["carrier_code"], bracket)]
            cause = ("new version" if live["dataset_version"] != current
                     else "same" if current in read_in else "scaled")  # fmt: skip
            off = (card.total - Decimal(live["total"])) / Decimal(live["total"]) * 100
            cases.append({"off_pct": round(float(off), 3), "cause": cause, "at": row["ts"][:19], "event": row["id"],
                          "lane": f"{live['origin']}-{live['destination']}", "airline": live["carrier_code"],
                          "kg": live["billed_kg"], "live": live["total"], "card": str(card.total),
                          "version": live["dataset_version"], "card_version": current})  # fmt: skip
    finally:
        calc.BILL_HIGHER_BREAK = bill_higher
        ctx.db.close()
    by_cause = {}
    for cause in ("same", "scaled", "new version"):
        offs = [abs(c["off_pct"]) for c in cases if c["cause"] == cause]
        if offs:
            by_cause[cause] = {"rates": len(offs), "worst_pct": max(offs)}
    return {
        "rates": len(rows),
        "priced": len(cases),
        "within_pct": {w: sum(abs(c["off_pct"]) <= float(w) for c in cases) for w in WITHIN},
        "by_cause": by_cause,
        "worst": sorted(cases, key=lambda c: -abs(c["off_pct"]))[:worst],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database")
    parser.add_argument("--worst", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(measure(args.database, args.worst), indent=1))


if __name__ == "__main__":
    main()
