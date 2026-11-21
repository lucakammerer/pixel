from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.crm import quotes
from pixel.ui.page import clock, esc, layout, page_head, stats, status

router = APIRouter()

VERDICT_CSS = {"accepted": "ok", "rejected": "bad"}


def pts(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _cell(text: str, title: str = "", css: str = "n", span: int = 1) -> str:
    attrs = (f' title="{esc(title)}"' if title else "") + (f" colspan={span}" if span > 1 else "")
    return f"<td class={css}{attrs}>{text}</td>"


def _muted(text: str) -> str:
    return f"<span class=muted>{esc(text)}</span>"


def _accuracy_time(r: dict[str, Any]) -> str:
    """Two cells: accuracy and time, or one spanning both while the round is being scored."""
    if "accuracy" in r["points"] or "time" in r["points"]:
        return _cell(pts(r["points"].get("accuracy"))) + _cell(pts(r["points"].get("time")))
    if r.get("verdict") == "accepted":
        return _cell(_muted("being scored"), "The scorer adds accuracy and time when the round closes", span=2)
    return _cell("—") + _cell("—")


def _cost(r: dict[str, Any]) -> str:
    value = r["points"].get("cost")
    if value is not None:
        return _cell(pts(value))
    if r["cost_estimate"] is not None:
        return _cell(
            _muted(pts(r["cost_estimate"])), "Estimated from our gateway spend: the scorer adds it after the hour"
        )
    if r.get("cost_pending"):
        return _cell(_muted("next hour"), "The scorer adds cost points once the hour is over")
    return _cell("—")


def _quote(r: dict[str, Any]) -> str:
    if r["quote"] is None:
        return _cell("—")
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
    estimate = s["cost_estimate"]
    rank = f"rank {s['rank']} · " if s["rank"] else ""
    quote_note = " + ".join(f"{k} {pts(p[k])}" for k in ("accuracy", "time", "cost") if k in p) + (
        f" + {pts(unreported)} accuracy and time the rows leave out" if unreported and unreported >= Decimal("0.01")
        else "")  # fmt: skip
    cost_note = (
        f"scored · {s['cost_pending']} rounds still due, estimated {pts(estimate)}"
        if s["cost_pending"] and estimate
        else f"scored · {s['cost_pending']} rounds still due"
        if s["cost_pending"]
        else "added by the scorer hourly"
    )
    humanity = p.get("humanity", Decimal(0))
    score = stats(
        [
            ("Team total", pts(total) if total is not None else None, f"{rank}from the scorer"),
            ("Projected total", pts(s["points_projected"]) if estimate and total is not None else None,
             "team total + the cost points still due"),
            ("Quote points", pts(s["points_quote"]), quote_note),
            ("Cost points", pts(p.get("cost", Decimal(0))), cost_note),
            ("Human writing", pts(humanity), "up to 10 per round · none awarded yet" if not humanity
             else "up to 10 per round"),
            ("Tracking points", pts(p.get("tracking", Decimal(0))), "10 per correct tracking answer"),
            ("Accepted by scorer", accepted, f'of {s["rounds"]} scored · {s["rejected"]} rejected'
             + (f' · {s["no_answer"]} no answer' if s["no_answer"] else "")),
            ("Spend per accepted quote", f"${spend:.4f}" if spend is not None else None,
             f'${float(s["spend_hour_usd"]):.4f} in the last hour'),
        ]
    )  # fmt: skip

    rounds = quotes.scored_rounds(ctx)
    adjustment = any("adjustment" in r["points"] for r in rounds)
    score_rows = "".join(
        f"<tr><td class=num>{esc(r.get('round'))}</td><td class=num>{clock(r.get('sent_at'))}</td>"
        f"<td class=n>{esc(round(float(r['seconds']))) if r.get('seconds') is not None else '—'}</td>"
        f"<td>{status(str(r.get('verdict') or '—'), VERDICT_CSS.get(r.get('verdict'), ''))}</td>"
        f"<td class=muted>{esc(r.get('reason') or '')}</td>"
        + _accuracy_time(r)
        + _cost(r)
        + _quote(r)
        + _cell(pts(r["points"].get("humanity")))
        + _cell(pts(r["points"].get("tracking")))
        + (_cell(pts(r["points"].get("adjustment"))) if adjustment else "")
        + _cell(f"<b>{pts(r['earned'])}</b>")
        + "</tr>"
        for r in rounds
    )
    extra_cols = 2 + adjustment
    cols = 5 + 4 + extra_cols + 1
    # rows being scored are not in the sum yet, so neither is their cost estimate
    due = sum((r["cost_estimate"] for r in rounds if r["earned"] is not None and r["cost_estimate"]), Decimal(0))
    gap = total + due - s["points_rows"] if total is not None else None
    note = ""
    if gap is not None and gap.copy_abs() >= Decimal("0.01"):
        note = f" · {pts(gap)} of the team total not in any row"
    elif total is not None:
        note = f" = team total {pts(total)}" + (f" + cost still due {pts(due)}" if due else "")
    if s["rounds_unknown"]:
        note += f" · {s['rounds_unknown']} {'round' if s['rounds_unknown'] == 1 else 'rounds'} still being scored"
    foot = (
        f"<tfoot><tr><td colspan={cols - 1} class=muted>Sum of the rows{note}</td>"
        f"<td class=n><b>{pts(s['points_rows'])}</b></td></tr></tfoot>"
        if rounds
        else ""
    )
    head = (
        "<tr><th colspan=5></th><th class=n colspan=4>The quote</th>"
        f"<th class=n colspan={extra_cols}>On top</th><th></th></tr>"
        "<tr><th>Round</th><th>Sent</th><th class=n>Seconds</th><th>Verdict</th><th>Reason</th>"
        "<th class=n>Accuracy</th><th class=n>Time</th><th class=n>Cost</th><th class=n>Quote</th>"
        "<th class=n>Humanity</th><th class=n>Tracking</th>"
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
