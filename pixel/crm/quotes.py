import json
import secrets
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pixel.contracts import Quote, QuoteStatus
from pixel.core.context import Context


def new_id(ctx: Context) -> str:
    return f"Q-{secrets.token_hex(4).upper()}"


def save(ctx: Context, quote: Quote) -> None:
    ctx.db.execute(
        "INSERT OR REPLACE INTO quotes (id, session_id, status, total, currency, fallback, llm_cost_usd, created_at,"
        " sent_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quote.id, quote.session_id, quote.status, str(quote.total), quote.currency, int(quote.fallback),
            str(quote.llm_cost_usd), quote.created_at.isoformat(), quote.sent_at.isoformat() if quote.sent_at else None,
            quote.model_dump_json(),
        ),
    )  # fmt: skip


def get(ctx: Context, quote_id: str) -> Quote | None:
    row = ctx.db.execute("SELECT data FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    return Quote.model_validate_json(row["data"]) if row else None


def for_session(ctx: Context, session_id: int) -> Quote | None:
    """The session's quote: the one sent if any, else the newest draft."""
    row = ctx.db.execute(
        "SELECT data FROM quotes WHERE session_id = ? ORDER BY sent_at IS NULL, created_at DESC LIMIT 1", (session_id,)
    ).fetchone()
    return Quote.model_validate_json(row["data"]) if row else None


def set_status(ctx: Context, quote_id: str, status: QuoteStatus) -> None:
    quote = get(ctx, quote_id)
    if quote:
        save(ctx, quote.model_copy(update={"status": status}))


def latest_scores(ctx: Context) -> list[dict[str, Any]]:
    """The newest `score` event per round, newest round first."""
    rows = ctx.db.execute(
        "SELECT payload FROM events WHERE id IN (SELECT MAX(id) FROM events WHERE kind = 'score'"
        " GROUP BY json_extract(payload, '$.round')) ORDER BY id DESC"
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def stats(ctx: Context, now: datetime | None = None) -> dict[str, Any]:
    """Dashboard numbers: requests, quoted, missed, accepted and rejected by the scorer, won and lost with the
    customer, points per kind, spend per accepted quote this hour, median response time."""
    now = now or datetime.now(UTC)
    db = ctx.db

    def sessions_with(*kinds: str) -> int:
        marks = ",".join("?" * len(kinds))
        return db.execute(f"SELECT COUNT(DISTINCT session_id) FROM events WHERE kind IN ({marks})", kinds).fetchone()[0]

    requests = db.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events WHERE (kind = 'route'"
        " AND json_extract(payload, '$.kind') = 'customer_rfq') OR kind IN ('rfq.extracted', 'quote.sent',"
        " 'quote.fallback', 'quote.missed')"
    ).fetchone()[0]
    status = dict(db.execute("SELECT status, COUNT(*) FROM quotes GROUP BY status").fetchall())

    scores = latest_scores(ctx)
    verdicts: dict[str, int] = {}
    points: dict[str, Decimal] = {}
    hour_ago = now - timedelta(hours=1)
    accepted_hour = 0
    for s in scores:
        verdicts[s.get("verdict", "")] = verdicts.get(s.get("verdict", ""), 0) + 1
        for kind, value in (s.get("points") or {}).items():
            points[kind] = points.get(kind, Decimal(0)) + Decimal(str(value or 0))
        sent = _parse(s.get("sent_at"))
        if s.get("verdict") == "accepted" and sent and sent >= hour_ago:
            accepted_hour += 1

    spend_hour = sum(
        (Decimal(r[0]) for r in db.execute("SELECT cost_usd FROM events WHERE ts >= ?", (hour_ago.isoformat(),))),
        Decimal(0),
    )
    seconds = []
    for row in db.execute("SELECT data FROM quotes WHERE sent_at IS NOT NULL"):
        q = Quote.model_validate_json(row["data"])
        seconds.append((q.sent_at - q.rfq.received_at).total_seconds())

    return {
        "requests": requests,
        "quoted": sessions_with("quote.sent", "quote.fallback"),
        "fallback": sessions_with("quote.fallback"),
        "missed": sessions_with("quote.missed"),
        "accepted": verdicts.get("accepted", 0),  # by the scorer (R26)
        "rejected": verdicts.get("rejected", 0),
        "pending": verdicts.get("pending", 0) + verdicts.get("open", 0),
        "won": status.get(QuoteStatus.ACCEPTED, 0),  # by the customer
        "lost": status.get(QuoteStatus.DECLINED, 0) + status.get(QuoteStatus.EXPIRED, 0),
        "points": points,
        "points_total": sum(points.values(), Decimal(0)),
        "spend_hour_usd": spend_hour,
        "spend_per_accepted_usd": (spend_hour / accepted_hour).quantize(Decimal("0.0001")) if accepted_hour else None,
        "median_response_s": round(statistics.median(seconds)) if seconds else None,
    }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
