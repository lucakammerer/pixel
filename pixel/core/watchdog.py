"""The watchdog: a customer request still without our quote STUCK_AFTER after the mail was stored calls
PIXEL_ALERT_URL once, with the step it hangs in, which starts a repair run.

It only reads the log and the mailbox's stored mail, and never touches the request or writes to a customer: the
orchestrator's deadline and fallback answer whatever happens here (R1)."""

import asyncio
import json
from datetime import datetime, timedelta

from pixel.contracts import MailKind
from pixel.core import events, feedback, outbox, timing
from pixel.core.context import Context
from pixel.core.orchestrator import EXPIRES_AFTER

STUCK_AFTER = timedelta(seconds=45)  # a normal quote leaves at about 33 s
POLL_S = 5


def step(ctx: Context, session_id: int, kind: str | None) -> str:
    """Where the request hangs, read from its events: route, extract, pickup, bids, pricing, compose or fallback.
    Compose covers the gateway's prose and translation, the check and the POST of the quote."""
    if kind is None:
        return "route"
    rows = ctx.db.execute(
        "SELECT kind, message_id, payload FROM events WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    seen = {r["kind"] for r in rows}
    if "quote.waiting" in seen:
        return "fallback"
    if "quote.drafted" in seen:
        return "compose"
    if "rfq.extracted" not in seen:
        return "extract"
    asked = {r["message_id"]: json.loads(r["payload"]).get("pickup") for r in rows if r["kind"] == "bid.requested"}
    for r in rows:
        if r["kind"] == "bid.received":
            asked.pop(json.loads(r["payload"]).get("request_id"), None)
    if any(asked.values()):
        return "pickup"
    if asked:
        return "bids"
    return "pricing" if "option.found" not in seen else "compose"


def _held(ctx: Context, session_id: int, now: datetime) -> bool:
    """The last reader holds this quote for a person and alerted that itself (`unmet.settle`); it leaves at the
    target without anyone."""
    row = ctx.db.execute(
        "SELECT json_extract(payload, '$.hold_until') FROM events WHERE session_id = ? AND kind = 'quote.unmet'"
        " ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return bool(row and row[0]) and datetime.fromisoformat(row[0]) > now


async def once(ctx: Context) -> int:
    """Alerts every request open longer than STUCK_AFTER that was not alerted yet. Returns how many."""
    now = timing.now()
    rows = ctx.db.execute(
        "SELECT m.session_id, m.kind, m.sent_at FROM messages m LEFT JOIN sessions s ON s.id = m.session_id"
        " WHERE m.direction = 'in' AND m.id = m.session_id AND (m.kind IS NULL OR m.kind = ?)"
        " AND m.sent_at > ? AND m.sent_at <= ? AND COALESCE(s.state, 'open') = 'open' AND COALESCE(s.mode, 'auto') = 'auto'"
        " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.session_id = m.session_id AND e.kind = 'quote.stuck')",
        (MailKind.CUSTOMER_RFQ.value, (now - EXPIRES_AFTER).isoformat(), (now - STUCK_AFTER).isoformat()),
    ).fetchall()  # fmt: skip
    issues = []
    for row in rows:
        session_id = row["session_id"]
        if outbox.quoted(ctx, session_id) or _held(ctx, session_id, now):
            continue
        age = round((now - datetime.fromisoformat(row["sent_at"])).total_seconds())
        hangs = step(ctx, session_id, row["kind"])
        events.emit(ctx, "quote.stuck", "watchdog", session_id=session_id, payload={"age_s": age, "step": hangs})
        issues.append({"issue": "stuck", "session": session_id, "age_s": age, "step": hangs,
                       "at": now.isoformat()[:19]})  # fmt: skip
    if issues and ctx.settings.alert_url:
        await feedback.post(ctx, issues)
    return len(issues)


async def run(ctx: Context) -> None:
    while True:
        try:
            await once(ctx)
        except Exception as exc:  # noqa: BLE001  the watchdog must never take the service down
            events.emit(ctx, "error", "watchdog", payload={"where": "watchdog", "error": repr(exc)[:200]})
        await asyncio.sleep(POLL_S)
