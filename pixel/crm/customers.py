"""Customers as the CRM sees them: one per sender address, built from sessions, quotes and shipments."""

from collections import Counter
from decimal import Decimal
from typing import Any

from pixel.contracts import Quote, QuoteStatus
from pixel.core.context import Context
from pixel.crm import shipments

LOST = (QuoteStatus.DECLINED, QuoteStatus.EXPIRED)


def _quotes(ctx: Context, customer: str | None = None) -> list[Quote]:
    """One quote per session (the one sent if any, else the newest draft), as `quotes.for_session` picks it."""
    rows = ctx.db.execute("SELECT data FROM quotes ORDER BY session_id, sent_at IS NULL, created_at DESC").fetchall()
    seen: set[int] = set()
    found = []
    for row in rows:
        q = Quote.model_validate_json(row["data"])
        if q.session_id in seen or (customer and q.rfq.customer.lower() != customer):
            continue
        seen.add(q.session_id)
        found.append(q)
    return found


def summary(ctx: Context) -> list[dict[str, Any]]:
    """Every customer, most recently active first: requests, quotes sent, won and lost with the customer,
    shipments, and the value of won quotes in EUR."""
    people: dict[str, dict[str, Any]] = {}

    def person(email: str) -> dict[str, Any]:
        key = email.lower()
        return people.setdefault(key, {
            "customer": key, "requests": 0, "quoted": 0, "won": 0, "lost": 0, "shipments": 0,
            "won_eur": Decimal(0), "last_seen": "", "lanes": Counter(),
        })  # fmt: skip

    for row in ctx.db.execute(
        "SELECT customer, MAX(received_at) AS last, COUNT(*) AS n FROM sessions GROUP BY LOWER(customer)"
    ):
        p = person(row["customer"])
        p["requests"] += row["n"]
        p["last_seen"] = max(p["last_seen"], row["last"] or "")
    for q in _quotes(ctx):
        p = person(q.rfq.customer)
        p["quoted"] += q.sent_at is not None
        p["won"] += q.status == QuoteStatus.ACCEPTED
        p["lost"] += q.status in LOST
        if q.status == QuoteStatus.ACCEPTED:
            p["won_eur"] += q.option.total_eur
        p["lanes"][f"{q.option.origin}-{q.option.destination}"] += 1
        p["last_seen"] = max(p["last_seen"], q.created_at.isoformat())
    for row in ctx.db.execute("SELECT customer, COUNT(*) AS n FROM shipments GROUP BY customer"):
        person(row["customer"])["shipments"] += row["n"]
    return sorted(people.values(), key=lambda p: p["last_seen"], reverse=True)


def profile(ctx: Context, customer: str) -> dict[str, Any] | None:
    """One customer's numbers plus their quotes and shipments, newest first; None when we never heard of them."""
    key = customer.lower()
    found = next((p for p in summary(ctx) if p["customer"] == key), None)
    if found is None:
        return None
    return {
        **found,
        "quotes": sorted(_quotes(ctx, key), key=lambda q: q.created_at, reverse=True),
        "shipments": shipments.find(ctx, customer=key),
    }
