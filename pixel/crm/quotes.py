import json
import secrets
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pixel.contracts import Quote, QuoteStatus
from pixel.core.context import Context

HIDDEN = ("accuracy", "time")  # the board may leave these out of each row until the scores are revealed
QUOTE = ("accuracy", "time", "cost")  # what the quote itself earns (R26 to R29)
# humanity (a reply the scorer finds human-written) and tracking (R30) come on top; the board's `booking` is always 0
COST_POINTS, COST_ZERO_USD = Decimal(40), Decimal("0.10")  # R28


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
        " GROUP BY CAST(json_extract(payload, '$.round') AS INTEGER))"
        " ORDER BY CAST(json_extract(payload, '$.round') AS INTEGER) DESC"
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def board_summary(ctx: Context) -> dict[str, Any]:
    """The newest team line of /v1/board/me: `total` (Decimal or None), `rank`, `revealed` (accuracy and time are
    in the request rows)."""
    row = ctx.db.execute("SELECT payload FROM events WHERE kind = 'score.total' ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(row["payload"]) if row else {}
    total = payload.get("total")
    return {
        "total": Decimal(str(total)) if total is not None else None,
        "rank": payload.get("rank"),
        "revealed": bool(payload.get("revealed")),
    }


def board_total(ctx: Context) -> Decimal | None:
    return board_summary(ctx)["total"]


def _number(value: Any) -> Decimal | None:
    try:
        return None if value is None or value == "" else Decimal(str(value))
    except ArithmeticError:
        return None


def _hour(value: Any) -> datetime | None:
    sent = _parse(value)
    return sent.astimezone(UTC).replace(minute=0, second=0, microsecond=0) if sent else None


def cost_estimates(ctx: Context, scores: list[dict[str, Any]]) -> dict[datetime, Decimal]:
    """Cost points per clock hour (UTC) the scorer has not added yet, as R28 computes them from our own log:
    COST_POINTS × (1 − gateway spend in the hour / accepted quotes in the hour / COST_ZERO_USD). An estimate: the
    scorer counts the spend at its gateway."""
    accepted: dict[datetime, int] = {}
    due: set[datetime] = set()
    for s in scores:
        hour = _hour(s.get("sent_at"))
        if hour is None or s.get("verdict") != "accepted":
            continue
        accepted[hour] = accepted.get(hour, 0) + 1
        if s.get("cost_pending") and _number((s.get("points") or {}).get("cost")) is None:
            due.add(hour)
    out = {}
    for hour in due:
        spend = sum(
            (Decimal(r[0]) for r in ctx.db.execute(
                "SELECT cost_usd FROM events WHERE ts >= ? AND ts < ?",
                (hour.isoformat(), (hour + timedelta(hours=1)).isoformat()),
            )),
            Decimal(0),
        )  # fmt: skip
        if not spend:
            continue  # no logged spend: no basis for an estimate (a quote without a gateway call earns no cost points)
        share = spend / accepted[hour] / COST_ZERO_USD
        out[hour] = (COST_POINTS * max(Decimal(0), 1 - share)).quantize(Decimal("0.01"))
    return out


def scored_rounds(ctx: Context) -> list[dict[str, Any]]:
    """`latest_scores` with what each round earned, split the way the scorer adds it up:
    `hidden` (accuracy + time; None while an accepted round is not scored yet: its verdict comes first),
    `quote` (accuracy + time + cost, the cost estimated while the scorer's is due), `cost_estimate`,
    `reported` (the points the row lists), `scored` (the row's total from the scorer; None while not scored) and
    `earned` (`scored` plus the cost estimate)."""
    scores = latest_scores(ctx)
    estimates = cost_estimates(ctx, scores)
    out = []
    for score in scores:
        points = {k: v for k, v in ((k, _number(v)) for k, v in (score.get("points") or {}).items()) if v is not None}
        reported = sum(points.values(), Decimal(0))
        hidden = None
        if any(k in points for k in HIDDEN):
            hidden = sum((points.get(k, Decimal(0)) for k in HIDDEN), Decimal(0))
        elif score.get("verdict") != "accepted":
            hidden = Decimal(0)  # the scorer gives a rejected or unanswered request no accuracy or time points
        total = _number(score.get("total"))
        if total is None and hidden is not None:
            total = reported
        estimate = None
        if score.get("cost_pending") and "cost" not in points and score.get("verdict") == "accepted":
            estimate = estimates.get(_hour(score.get("sent_at")))
        quote = None
        if hidden is not None:
            quote = hidden + (points["cost"] if "cost" in points else estimate or Decimal(0))
        out.append(score | {
            "points": points, "reported": reported, "hidden": hidden, "quote": quote, "cost_estimate": estimate,
            "scored": total, "earned": total + estimate if total is not None and estimate is not None else total,
        })  # fmt: skip
    return out


def stats(ctx: Context, now: datetime | None = None) -> dict[str, Any]:
    """Dashboard numbers: requests, quoted, missed, accepted and rejected by the scorer, won and lost with the
    customer, the team total and how it splits into the points each request row reports and the accuracy and time
    the board hides until the reveal, spend per accepted quote this hour, median response time."""
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

    board = board_summary(ctx)
    rounds = scored_rounds(ctx)
    verdicts: dict[str, int] = {}
    points: dict[str, Decimal] = {}
    hour_ago = now - timedelta(hours=1)
    accepted_hour = cost_pending = 0
    for s in rounds:
        verdicts[s.get("verdict") or ""] = verdicts.get(s.get("verdict") or "", 0) + 1
        for kind, value in s["points"].items():
            points[kind] = points.get(kind, Decimal(0)) + value
        cost_pending += bool(s.get("cost_pending")) and "cost" not in s["points"]
        sent = _parse(s.get("sent_at"))
        if s.get("verdict") == "accepted" and sent and sent >= hour_ago:
            accepted_hour += 1
    reported = sum(points.values(), Decimal(0))
    total = board["total"]
    known = [s["earned"] for s in rounds if s["earned"] is not None]
    estimate = sum((s["cost_estimate"] for s in rounds if s["cost_estimate"] is not None), Decimal(0))
    unreported = total - reported if total is not None else None

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
        "rounds": len(rounds),
        "accepted": verdicts.get("accepted", 0),  # by the scorer (R26)
        "rejected": verdicts.get("rejected", 0),
        "no_answer": verdicts.get("no answer", 0),
        "won": status.get(QuoteStatus.ACCEPTED, 0),  # by the customer
        "lost": status.get(QuoteStatus.DECLINED, 0) + status.get(QuoteStatus.EXPIRED, 0),
        "points": points,  # per kind, summed over the rows
        "points_reported": reported,
        "points_total": total,  # the board's team total
        "points_unreported": unreported,  # accuracy + time the rows leave out, until the reveal
        "points_quote": sum((points.get(k, Decimal(0)) for k in QUOTE), Decimal(0)) + (unreported or 0),
        "cost_estimate": estimate,  # cost points the scorer has not added yet, from our own spend
        "points_projected": total + estimate if total is not None else None,
        "points_rows": sum(known, Decimal(0)),
        "rounds_unknown": len(rounds) - len(known),
        "rank": board["rank"],
        "revealed": board["revealed"],
        "cost_pending": cost_pending,
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


def recent(ctx: Context, limit: int = 300) -> list[Quote]:
    """Every quote, newest first."""
    rows = ctx.db.execute("SELECT data FROM quotes ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [Quote.model_validate_json(r["data"]) for r in rows]
