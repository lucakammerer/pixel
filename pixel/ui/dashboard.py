from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.crm import quotes
from pixel.ui.page import clock, esc, layout, page_head, stats, status

router = APIRouter()

VERDICT_CSS = {"accepted": "ok", "rejected": "bad"}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """`crm.quotes.stats` and the latest `score` events. Shipments live in the CRM."""
    ctx = request.app.state.ctx
    s = quotes.stats(ctx)
    spend = s["spend_per_accepted_usd"]
    quoted_share = f"{round(100 * s['quoted'] / s['requests'])} % of requests" if s["requests"] else ""
    flow = stats([
        ("Requests", s["requests"], ""),
        ("Quoted", s["quoted"], quoted_share),
        ("By fallback", s["fallback"], "sent at the deadline"),
        ("Missed", s["missed"], ""),
        ("Median response", f'{s["median_response_s"]} s' if s["median_response_s"] is not None else None, ""),
        ("Won with customer", s["won"], f'{s["lost"]} lost'),
    ])  # fmt: skip
    score = stats(
        [
            ("Points", f"{float(s['points_total']):.1f}", ""),
            ("Accepted by scorer", s["accepted"], f'{s["rejected"]} rejected · {s["pending"]} pending'),
            ("Spend per accepted quote", f"${spend:.4f}" if spend is not None else None,
             f'${float(s["spend_hour_usd"]):.4f} in the last hour'),
        ]
        + [(f"{k.capitalize()} points", f"{float(v):.1f}", "") for k, v in sorted(s["points"].items())]
    )  # fmt: skip

    score_rows = "".join(
        f"<tr><td class=num>{esc(r.get('round'))}</td><td class=num>{clock(r.get('sent_at'))}</td>"
        f"<td>{status(str(r.get('verdict') or '—'), VERDICT_CSS.get(r.get('verdict'), ''))}</td>"
        f"<td class=muted>{esc(r.get('reason') or '')}</td>"
        + "".join(f"<td class=n>{esc((r.get('points') or {}).get(k, ''))}</td>" for k in ("accuracy", "cost", "time"))
        + f"<td class=n><b>{esc(r.get('total', ''))}</b></td></tr>"
        for r in quotes.latest_scores(ctx)[:30]
    )
    body = (
        page_head("Performance", "Our pipeline and what the scorer says about it. Updates live.")
        + "<div id=refresh data-live>"
        + f"<h2 style='margin-top:0'>Pipeline</h2>{flow}<h2>Score</h2>{score}"
        + "<h2>Scorer, per request</h2><div class=panel><table><thead><tr><th>Round</th><th>Sent</th>"
        "<th>Verdict</th><th>Reason</th><th class=n>Accuracy</th><th class=n>Cost</th><th class=n>Time</th>"
        "<th class=n>Total</th></tr></thead>"
        f"<tbody>{score_rows or '<tr><td colspan=8 class=empty>No result from /v1/board/me yet.</td></tr>'}</tbody>"
        "</table></div></div>"
    )
    return layout("Performance", body, nav="dashboard", script="live('/events',refresh());")
