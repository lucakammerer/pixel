from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.crm import quotes
from pixel.ui.page import clock, esc, layout, page_head, stats, status

router = APIRouter()

VERDICT_CSS = {"accepted": "ok", "rejected": "bad"}
DERIVED = "Change of the team total when the verdict appeared, less the points the row reports"


def pts(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _cell(text: str, title: str = "", css: str = "n", span: int = 1) -> str:
    attrs = (f' title="{esc(title)}"' if title else "") + (f" colspan={span}" if span > 1 else "")
    return f"<td class={css}{attrs}>{text}</td>"


def _muted(text: str) -> str:
    return f"<span class=muted>{esc(text)}</span>"


def _accuracy_time(r: dict[str, Any]) -> str:
    """Two cells: accuracy and time, or one spanning both while the board hides them."""
    if "accuracy" in r["points"] or "time" in r["points"]:
        return _cell(pts(r["points"].get("accuracy"))) + _cell(pts(r["points"].get("time")))
    if r.get("verdict") != "accepted":
        return _cell("—") + _cell("—")
    if r["hidden"] is not None:
        return _cell(f"{pts(r['hidden'])} {_muted('together')}", DERIVED, span=2)
    return _cell(_muted("hidden"), "Not derivable: another verdict or no total in the same poll", span=2)


def _cost(r: dict[str, Any]) -> str:
    value = r["points"].get("cost")
    if value is not None:
        return _cell(pts(value))
    if r["cost_estimate"] is not None:
        return _cell(f"≈ {pts(r['cost_estimate'])}", "Due from the scorer next hour; estimated from our gateway spend")
    if r.get("cost_pending"):
        return _cell(_muted("next hour"), "The scorer adds cost points once the hour is over")
    return _cell("—")


def _quote(r: dict[str, Any]) -> str:
    if r["quote"] is None:
        return _cell("—")
    if r["cost_estimate"] is not None:
        return _cell(f"<b>{pts(r['quote'])}</b> {_muted('+ ≈ ' + pts(r['cost_estimate']))}")
    return _cell(f"<b>{pts(r['quote'])}</b>")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """`crm.quotes.stats` and the scored rounds of /v1/board/me. Shipments live in the CRM."""
    ctx = request.app.state.ctx
    s = quotes.stats(ctx)
    spend = s["spend_per_accepted_usd"]
    total = s["points_total"]
    quoted_share = f"{round(100 * s['quoted'] / s['requests'])} % of requests" if s["requests"] else ""
    flow = stats([
        ("Requests", s["requests"], ""),
        ("Quoted", s["quoted"], quoted_share),
        ("By fallback", s["fallback"], "sent at the deadline"),
        ("Missed", s["missed"], ""),
        ("Median response", f'{s["median_response_s"]} s' if s["median_response_s"] is not None else None, ""),
        ("Won with customer", s["won"], f'{s["lost"]} lost'),
    ])  # fmt: skip

    p, accepted = s["points"], s["accepted"]
    unreported = s["points_unreported"]
    rank = f"rank {s['rank']} · " if s["rank"] else ""
    quote_note = " + ".join(
        f"{k} {pts(p[k])}" for k in ("accuracy", "time", "cost", "humanity") if k in p
    ) + (f" + {pts(unreported)} accuracy and time the rows leave out" if unreported and unreported >= Decimal("0.01")
         else "")  # fmt: skip
    after_note = f"tracking {pts(p.get('tracking'))} · booking {pts(p.get('booking'))}"
    cost_note = (
        f"{s['cost_pending']} rounds due · ≈ {pts(s['cost_estimate'])} from our spend"
        if s["cost_pending"]
        else "added by the scorer once an hour"
    )
    score = stats(
        [
            ("Team total", pts(total) if total is not None else None, f"{rank}from the scorer"),
            ("Quote points", pts(s["points_quote"]), quote_note),
            ("Tracking + booking", pts(s["points_after"]), after_note),
            ("Cost points", pts(p.get("cost", Decimal(0))), cost_note),
            ("Projected total", pts(s["points_projected"]) if s["cost_estimate"] and total is not None else None,
             "team total + the cost points still due"),
            ("Accepted by scorer", accepted, f'of {s["rounds"]} scored · {s["rejected"]} rejected'
             + (f' · {s["no_answer"]} no answer' if s["no_answer"] else "")),
            ("Spend per accepted quote", f"${spend:.4f}" if spend is not None else None,
             f'${float(s["spend_hour_usd"]):.4f} in the last hour'),
        ]
    )  # fmt: skip

    rounds = quotes.scored_rounds(ctx)
    extra = [k for k in ("humanity", "adjustment") if any(k in r["points"] for r in rounds)]
    humanity = "humanity" in extra
    adjustment = "adjustment" in extra
    score_rows = "".join(
        f"<tr><td class=num>{esc(r.get('round'))}</td><td class=num>{clock(r.get('sent_at'))}</td>"
        f"<td class=n>{esc(round(float(r['seconds']))) if r.get('seconds') is not None else '—'}</td>"
        f"<td>{status(str(r.get('verdict') or '—'), VERDICT_CSS.get(r.get('verdict'), ''))}</td>"
        f"<td class=muted>{esc(r.get('reason') or '')}</td>"
        + _accuracy_time(r)
        + _cost(r)
        + (_cell(pts(r["points"].get("humanity"))) if humanity else "")
        + _quote(r)
        + _cell(pts(r["points"].get("tracking")))
        + _cell(pts(r["points"].get("booking")))
        + (_cell(pts(r["points"].get("adjustment"))) if adjustment else "")
        + _cell(f"<b>{pts(r['earned'])}</b>")
        + "</tr>"
        for r in rounds
    )
    quote_cols = 4 + humanity
    after_cols = 2 + adjustment
    cols = 5 + quote_cols + after_cols + 1
    gap = total - s["points_rows"] if total is not None else None
    foot = (
        f"<tfoot><tr><td colspan={cols - 1} class=muted>Sum of the rows with a known total"
        + ((f" · the other {s['rounds_unknown']} rounds, accuracy + time still hidden: {pts(gap)}"
            if s["rounds_unknown"] else f" · {pts(gap)} of the team total not in any row")
           if gap is not None and gap.copy_abs() >= Decimal("0.01") else "")
        + f"</td><td class=n><b>{pts(s['points_rows'])}</b></td></tr></tfoot>"
        if rounds
        else ""
    )  # fmt: skip
    head = (
        "<tr><th colspan=5></th>"
        f"<th class=n colspan={quote_cols}>The quote</th>"
        f"<th class=n colspan={after_cols}>After the quote</th><th></th></tr>"
        "<tr><th>Round</th><th>Sent</th><th class=n>Seconds</th><th>Verdict</th><th>Reason</th>"
        "<th class=n>Accuracy</th><th class=n>Time</th><th class=n>Cost</th>"
        + ("<th class=n>Humanity</th>" if humanity else "")
        + "<th class=n>Quote</th><th class=n>Tracking</th><th class=n>Booking</th>"
        + ("<th class=n>Adjustment</th>" if adjustment else "")
        + "<th class=n>Total</th></tr>"
    )
    body = (
        page_head("Performance", "Our pipeline and what the scorer says about it. Updates live.")
        + "<div id=refresh data-live>"
        + f"<h2 style='margin-top:0'>Pipeline</h2>{flow}<h2>Score</h2>{score}"
        + f"<h2>Scorer, per round</h2><div class=panel><table><thead>{head}</thead>"
        f"<tbody>{score_rows or f'<tr><td colspan={cols} class=empty>No result from /v1/board/me yet.</td></tr>'}"
        f"</tbody>{foot}</table></div></div>"
    )
    return layout("Performance", body, nav="dashboard", script="live('/events',refresh());")
