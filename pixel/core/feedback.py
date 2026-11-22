"""The feedback loop: what each request scored, why it lost points, and what that teaches the next quote.

`run` polls GET /v1/board/me. A request's verdict appears as soon as we answer; its accuracy and time only once the
round closes and is scored, so a request is explained when its row carries them (or, rejected or unanswered, when its
verdict is final). `explain` works back from those points to the scorer's best price and looks for it among
the options the session had; the result is a `feedback` event in that session. For a door-to-port request it names
the trucker the scorer took, and `pickup_rule` picks the balanced rule that agrees with those most often.

What the service cannot correct itself goes out: `attention` lists what went wrong lately, `alert` calls
PIXEL_ALERT_URL with what cost or may cost points, once per session (an issue without one: once per kind and
deployed commit) and only for what happened since this process started. An alert carries codes and numbers only:
the scorer's reason, the round, our quoted and the best price where known, never text from a mail.
"""

import asyncio
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from pixel.contracts import RateOption
from pixel.core import db, events
from pixel.core.context import Context
from pixel.crm import quotes
from pixel.pricing import selection, session
from tools.board_me import normalize, rounds_of

POLL_S = 10
ACCURACY, TIME = Decimal(45), Decimal(15)
FULL_S, EXPIRES_S = 120, 360  # R29
ZERO_AT = Decimal("0.10")  # accuracy reaches 0 at this distance from the best price; fitted on scored requests
FINAL = {"accepted", "rejected", "no answer"}
STATE = "feedback.state"
ALERTED = "feedback.alerted"
ALERT_MINUTES = 15
RULE_LEAD = 2  # explained requests a rule must lead the default by before it is used
VERSION = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"], check=False, capture_output=True, text=True
).stdout.strip()
STARTED = datetime.now(UTC).isoformat()[:19]  # this commit answers for what happens from here on
REPAIRABLE = {
    "missed",
    "rejected",
    "no_answer",
    "lost_points",
    "tracking_unmatched",
    "error",
    "quote_doubtful",
}


def time_points(seconds: float) -> Decimal:
    if seconds <= FULL_S:
        return TIME
    return max(Decimal(0), TIME * Decimal(EXPIRES_S - seconds) / Decimal(EXPIRES_S - FULL_S))


def explain(points: Decimal, seconds: float, quoted: RateOption, options: list[RateOption]) -> dict[str, Any]:
    """Accuracy points, the points lost, and the option (airline, trucker) whose total is the scorer's best price.
    The distance from the best price counts in both directions, so each loss fits two best prices; `best` is
    None when neither is a total the session could have quoted."""
    accuracy = points - time_points(seconds)
    lost = ACCURACY - accuracy
    result: dict[str, Any] = {"accuracy": accuracy, "lost": max(lost, Decimal(0)), "best": None}
    if not Decimal(0) <= accuracy <= ACCURACY:
        return result
    if lost < Decimal("0.05"):
        return result | {"best": quoted.total_eur, "carrier": quoted.carrier, "trucker": quoted.trucker}
    off = lost / ACCURACY * ZERO_AT
    airlines = [o for o in options if o.trucker is None]
    pickups: list[RateOption | None] = [o for o in options if o.trucker is not None] if quoted.trucker else []
    for best in (quoted.total_eur / (1 + off), quoted.total_eur / (1 - off)):
        for airline in airlines:
            for pickup in pickups or [None]:
                total = airline.total_eur + (pickup.total_eur if pickup else 0)
                if abs(total - best) <= total * Decimal("0.0001") + Decimal("0.05"):
                    return result | {"best": total, "carrier": airline.carrier, "billed_kg": airline.billed_kg,
                                     "trucker": pickup.trucker if pickup else None}  # fmt: skip
    return result


def pickup_rule(ctx: Context) -> str:
    """The balanced rule (`selection.RULES`) that picks the scorer's trucker in the most explained requests. It
    replaces the default only when it picks the scorer's trucker in RULE_LEAD more requests: one misread round
    must not change how every next door-to-port request is priced."""
    rows = ctx.db.execute(
        "SELECT session_id, json_extract(payload, '$.trucker') AS trucker FROM events"
        " WHERE kind = 'feedback' AND json_extract(payload, '$.trucker') IS NOT NULL"
    ).fetchall()
    hits = dict.fromkeys(selection.RULES, 0)
    try:
        for row in rows:
            bids = [o for o in session.options(ctx, row["session_id"]) if o.trucker and o.pickup_hours is not None]
            for rule in selection.RULES if len(bids) > 1 else ():
                hits[rule] += selection.balanced(bids, lambda o: o.pickup_hours, rule).trucker == row["trucker"]
    except Exception:  # noqa: BLE001  a stored option we cannot read must not cost the quote: keep the default rule
        return next(iter(selection.RULES))
    default = next(iter(selection.RULES))
    best = max(hits, key=lambda rule: hits[rule])
    return best if hits[best] >= hits[default] + RULE_LEAD else default


def _session(ctx: Context, sent_at: str) -> int | None:
    asked = datetime.fromisoformat(sent_at)
    rows = ctx.db.execute("SELECT id, received_at FROM sessions ORDER BY id DESC LIMIT 100").fetchall()
    near = [(abs((datetime.fromisoformat(r["received_at"]) - asked).total_seconds()), r["id"]) for r in rows]
    return min(near)[1] if near and min(near)[0] <= 10 else None


def _quoted(ctx: Context, session_id: int) -> RateOption | None:
    quote = quotes.for_session(ctx, session_id)
    if quote and quote.sent_at is not None:
        return quote.option
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE session_id = ? AND kind = 'quote.drafted' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return session.option(json.loads(row["payload"])["option"]) if row else None


def _scored(row: dict[str, Any]) -> Decimal | None:
    """Accuracy + time of a scored row; None while the round is not scored yet."""
    shown = normalize(row)["points"]
    if not any(k in shown for k in ("accuracy", "time")):
        return None
    return sum((Decimal(str(shown.get(k) or 0)) for k in ("accuracy", "time")), Decimal(0))


def _settled(row: dict[str, Any]) -> bool:
    return row.get("verdict") in FINAL and (row["verdict"] != "accepted" or _scored(row) is not None)


def _round(ctx: Context, row: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"round": row["round_no"], "verdict": row["verdict"], "reason": row.get("reason")}
    points = _scored(row)
    if points is not None:
        payload["hidden"] = points  # accuracy + time
    session_id = _session(ctx, row["sent_at"])
    quoted = _quoted(ctx, session_id) if session_id else None
    if row["verdict"] == "accepted" and quoted and points is not None and row.get("answered_at"):
        seconds = (datetime.fromisoformat(row["answered_at"]) - datetime.fromisoformat(row["sent_at"])).total_seconds()
        options = session.options(ctx, session_id)
        fit = explain(points, seconds, quoted, options) | {"points": points}
        payload |= {"seconds": round(seconds), "quoted": quoted.total_eur, "quoted_trucker": quoted.trucker} | fit
    events.emit(ctx, "feedback", "board", session_id=session_id,
                payload=json.loads(json.dumps(payload, default=str)))  # fmt: skip


def _standing(public: dict[str, Any], team: str | None) -> dict[str, Any]:
    """From the public board: how many teams, and the teams just above and below ours (rank, name, total)."""
    ranking = public.get("ranking") or []
    ours = next((i for i, r in enumerate(ranking) if r.get("name") == team), None)
    if ours is None:
        return {"teams": len(ranking)} if ranking else {}

    def side(i: int) -> dict[str, Any] | None:
        r = ranking[i] if 0 <= i < len(ranking) else None
        return {"rank": r.get("rank"), "team": r.get("team"), "total": str(r.get("total"))} if r else None

    return {"teams": len(ranking), "above": side(ours - 1), "below": side(ours + 1)}


async def once(ctx: Context) -> int:
    """Store board totals and finalized score updates, and return the number of new feedback verdicts."""
    data = await ctx.board.me()
    reported_total = data.get("total")
    total = Decimal(str(reported_total)) if reported_total is not None else None
    try:
        public = await ctx.board.public()
    except Exception:  # noqa: BLE001  the public board is for the dashboard only: never cost the poll
        public = {}
    summary = {"total": str(total) if total is not None else None, "rank": data.get("rank"),
               "revealed": bool(data.get("scores_revealed"))} | _standing(public, data.get("team"))  # fmt: skip
    info = rounds_of(public)
    if total is not None and summary != quotes.board_summary(ctx)["raw"]:
        events.emit(ctx, "score.total", "board", payload=summary)
    requests = [r for r in data.get("requests", []) if r.get("verdict") in FINAL]
    stored = {str(score["round"]): score for score in quotes.latest_scores(ctx)}
    for row in requests:
        score = normalize(row) | {"round": row["round_no"]} | info.get(str(row["round_no"]), {})
        score = json.loads(json.dumps(score, default=str))
        if stored.get(str(score["round"])) != score:
            events.emit(ctx, "score", "board", payload=score)
            stored[str(score["round"])] = score
    state = json.loads(db.get(ctx.db, STATE) or '{"total": null, "verdicts": {}}')
    # an accepted round counts as new once it is scored: its verdict alone comes before its points
    new = [r for r in requests if _settled(r) and state["verdicts"].get(str(r["round_no"])) != r["verdict"]]
    first = state["total"] is None  # the first poll: remember the verdicts, explain nothing
    for row in [] if first else new:
        _round(ctx, row)
    state["verdicts"].update({str(r["round_no"]): r["verdict"] for r in new})
    db.put(ctx.db, STATE, json.dumps({"total": str(total) if total is not None else None,
                                      "verdicts": state["verdicts"]}))  # fmt: skip
    return 0 if first else len(new)


def attention(conn: sqlite3.Connection, minutes: int) -> list[dict[str, Any]]:
    """What went wrong in the last minutes, newest first: missed, stuck, rejected and point-losing requests, tracking
    questions we matched to no shipment, customer replies left to a person, errors, bids read implausibly low."""
    since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    rows = conn.execute(
        "SELECT id, ts, session_id, kind, payload FROM events WHERE ts > ? AND kind IN"
        " ('quote.missed', 'quote.stuck', 'feedback', 'tracking.answered', 'error', 'human.note', 'quote.checked', 'option.failed',"
        " 'rfq.novelty')"
        " ORDER BY id DESC",
        (since,)
    ).fetchall()  # fmt: skip
    issues = []
    for row in rows:
        p, issue = json.loads(row["payload"]), None
        if row["kind"] == "quote.missed":
            issue = "missed"
        elif row["kind"] == "quote.stuck":
            issue = "stuck"
        elif row["kind"] == "feedback" and p.get("verdict") != "accepted":
            issue = str(p.get("verdict")).replace(" ", "_")
        elif row["kind"] == "feedback" and Decimal(str(p.get("lost") or 0)) >= 1:
            issue = "lost_points"
        elif row["kind"] == "tracking.answered" and not p.get("shipments"):
            issue = "tracking_unmatched"
        elif row["kind"] == "error" and p.get("where") not in ("alert", "poll"):  # not ours: the hook, the API
            issue = "error"
        elif row["kind"] == "human.note" and p.get("note"):
            issue = "reply_unhandled"
        elif row["kind"] == "quote.checked" and p.get("blocking"):
            issue = "quote_doubtful"
        elif row["kind"] == "option.failed" and p.get("error") == "implausible":
            issue = "bid_distrusted"
        elif row["kind"] == "rfq.novelty" and any((p.get("mapped") or {}).values()):
            issue = "novel_requirement"  # already posted by `novelty.check` the moment it was read
        if issue:
            issues.append({"issue": issue, "event": row["id"], "at": row["ts"][:19], "session": row["session_id"],
                           "detail": p})  # fmt: skip
    return issues


def _code(value: Any) -> str | None:
    code = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    return code if re.fullmatch(r"[a-z0-9_]{1,40}", code) else None


def _brief(ctx: Context, issue: dict[str, Any]) -> dict[str, Any]:
    """The alert line of one issue: codes and numbers only, never free text from a mail."""
    p, session_id = issue["detail"], issue["session"]
    quoted = p.get("quoted")
    if quoted is None and session_id and issue["issue"] in ("rejected", "no_answer", "lost_points"):
        option = _quoted(ctx, session_id)
        quoted = option.total_eur if option else None
    best = p.get("best")
    reason = _code(p.get("reason")) if issue["issue"] != "missed" else "missed"
    if reason is None and quoted is not None and best is not None and Decimal(str(quoted)) != Decimal(str(best)):
        reason = "too_high" if Decimal(str(quoted)) > Decimal(str(best)) else "too_low"
    return {"issue": issue["issue"], "session": session_id, "at": issue["at"],
            "reason": reason or _code(p.get("verdict")) or issue["issue"], "round": p.get("round"),
            "quoted": str(quoted) if quoted is not None else None,
            "best": str(best) if best is not None else None}  # fmt: skip


async def alert(ctx: Context) -> int:
    """Reports each lost session once (an issue without a session once per kind and commit). Returns how many."""
    if not ctx.settings.alert_url:
        return 0
    sent = json.loads(db.get(ctx.db, ALERTED) or "{}")

    def key(i: dict[str, Any]) -> str:
        return f"session:{i['session']}" if i["session"] else f"{i['issue']}@{VERSION}"

    new = {key(i): i for i in reversed(attention(ctx.db, ALERT_MINUTES))
           if i["issue"] in REPAIRABLE and i["at"] >= STARTED and key(i) not in sent}  # fmt: skip
    if not new:
        return 0
    sent.update({k: i["at"] for k, i in new.items()})
    db.put(ctx.db, ALERTED, json.dumps(sent))  # before the call: a hook that fails must not be called every poll
    await post(ctx, [_brief(ctx, i) for i in new.values()])
    return len(new)


async def post(ctx: Context, issues: list[dict[str, Any]]) -> None:
    """Calls PIXEL_ALERT_URL, which starts a repair run; also used by `watchdog`."""
    body = {"key": "pixel-repair", "version": VERSION, "issues": issues}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            (await client.post(ctx.settings.alert_url, json=body)).raise_for_status()
    except httpx.HTTPError as exc:
        events.emit(ctx, "error", "board", payload={"where": "alert", "error": type(exc).__name__})


async def run(ctx: Context) -> None:
    while True:
        try:
            await once(ctx)
            await alert(ctx)
        except Exception as exc:  # noqa: BLE001  feedback must never take the service down
            events.emit(ctx, "error", "board", payload={"where": "feedback", "error": repr(exc)[:200]})
        await asyncio.sleep(POLL_S)
