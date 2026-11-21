"""Carriers as the CRM sees them: one scorecard per airline or trucker, built from the event log, quotes and
bookings."""

import json
from datetime import datetime
from statistics import median
from typing import Any

from pixel.contracts import BookingStatus, Quote
from pixel.core.context import Context
from pixel.crm import bookings


def scorecard(ctx: Context) -> list[dict[str, Any]]:
    """Every carrier: lanes known (its sheets and the newest list it gave when declining), bid requests sent and
    skipped, answers and how many were priced, median answer time, how often it was the scorer's best price and the
    one we quoted, confirmed bookings. Busiest first."""
    db = ctx.db
    cards: dict[str, dict[str, Any]] = {}

    def card(name: str, kind: str = "airline") -> dict[str, Any]:
        return cards.setdefault(name, {
            "carrier": name, "kind": kind, "lanes": set(), "learned_at": None, "requested": 0, "skipped": 0,
            "answers": 0, "priced": 0, "seconds": [], "best": 0, "quoted": 0, "confirmed": 0,
        })  # fmt: skip

    for row in db.execute("SELECT name, kind FROM carriers"):
        card(row["name"], row["kind"])
    for row in db.execute("SELECT DISTINCT carrier, origin, destination FROM tariffs"):
        card(row["carrier"])["lanes"].add(f"{row['origin']}-{row['destination']}")
    asked: dict[int, str] = {}
    learned: dict[str, tuple[str, list[str]]] = {}
    rows = db.execute(
        "SELECT ts, kind, message_id, payload FROM events WHERE kind IN "
        "('bid.requested', 'bid.skipped', 'bid.received', 'carrier.lanes', 'feedback') ORDER BY id"
    )
    for row in rows:
        p, kind = json.loads(row["payload"]), row["kind"]
        if kind == "feedback":
            for name in {p.get("carrier"), p.get("trucker")} - {None}:
                card(name)["best"] += 1
        elif kind == "carrier.lanes":
            learned[p["carrier"]] = (row["ts"], p["lanes"])
        elif "carrier" in p:
            c = card(p["carrier"])
            if kind == "bid.requested":
                c["requested"] += 1
                asked[row["message_id"]] = row["ts"]
            elif kind == "bid.skipped":
                c["skipped"] += 1
            else:
                c["answers"] += 1
                c["priced"] += bool(p.get("priced"))
                if (sent := asked.get(p.get("request_id"))) is not None:
                    seconds = (datetime.fromisoformat(row["ts"]) - datetime.fromisoformat(sent)).total_seconds()
                    c["seconds"].append(seconds)
    for name, (ts, lanes) in learned.items():
        card(name).update(learned_at=ts)
        cards[name]["lanes"].update(lanes)
    for row in db.execute("SELECT data FROM quotes WHERE sent_at IS NOT NULL"):
        option = Quote.model_validate_json(row["data"]).option
        for name in {option.carrier, option.trucker} - {None}:
            card(name)["quoted"] += 1
    for b in bookings.all_by_id(ctx).values():
        if b.status == BookingStatus.CONFIRMED:
            card(b.carrier)["confirmed"] += 1
    for c in cards.values():
        c["median_s"] = round(median(c.pop("seconds"))) if c["seconds"] else None
    return sorted(cards.values(), key=lambda c: (-c["requested"] - c["quoted"], c["kind"], c["carrier"]))
