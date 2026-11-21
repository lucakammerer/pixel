from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pixel.crm import quotes
from pixel.ui import home, names
from pixel.ui.page import clock, company, customer_href, esc, layout, money, page_head, status

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


CSS = """<style>
.hero{display:flex;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.hero>div{padding:18px 24px;border-right:1px solid var(--line);display:flex;flex-direction:column;justify-content:center}
.hero>div:last-child{border-right:0;flex:1}
.hero .k{font-size:12px;color:var(--muted)}
.hero .rank{font-size:48px;font-weight:500;line-height:1;letter-spacing:-.035em;font-variant-numeric:tabular-nums}
.hero .rank small{font-size:14px;font-weight:500;color:var(--muted);letter-spacing:0;margin-left:6px}
.hero .total{font-size:28px;font-weight:500;line-height:1.15;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.hero .note{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.hero .race{font-size:13px;color:var(--ink-2);font-variant-numeric:tabular-nums;line-height:1.8}
.hero .race b{font-weight:500}.hero .race .behind{color:var(--bad)}.hero .race .ahead{color:var(--ok)}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));background:var(--surface);
border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:10px}
.strip>div{padding:10px 14px;border-right:1px solid var(--line)}.strip>div:last-child{border-right:0}
.strip .k{font-size:12px;color:var(--muted)}
.strip .v{font-size:16px;font-weight:500;letter-spacing:-.01em;font-variant-numeric:tabular-nums;white-space:nowrap}
.strip .note{font-size:12px;color:var(--muted);white-space:nowrap}
.rounds{overflow-x:auto;max-width:100%}
.duo{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:24px;align-items:start}
.feed .item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;padding:11px 16px;
border-bottom:1px solid var(--line)}.feed .item:last-child{border-bottom:0}
.feed .what{color:var(--ink-2);font-size:13px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.feed .nm{font-weight:500;color:var(--ink)}.feed .t{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
h2.section{font-size:20px;letter-spacing:-.015em;margin-top:56px;padding-top:32px;border-top:1px solid var(--line)}
.biz .strip{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
.biz .strip>div{border-bottom:1px solid var(--line);margin-bottom:-1px}.biz .strip .note{white-space:normal}
@media (max-width:900px){.duo{grid-template-columns:1fr}}.rounds td,.rounds th{white-space:nowrap}
@media (max-width:640px){.hero>div{border-right:0;border-bottom:1px solid var(--line);flex:1 1 100%;padding:14px 16px}
.hero .rank{font-size:40px}}
</style>"""


def _strip(cells: list[tuple[str, Any, str]]) -> str:
    return "<div class=strip>" + "".join(
        f"<div><div class=k>{esc(k)}</div><div class=v>{esc(v)}</div>"
        + (f"<div class=note>{esc(note)}</div>" if note else "") + "</div>"
        for k, v, note in cells
    ) + "</div>"  # fmt: skip


def _hero(s: dict[str, Any]) -> str:
    """Rank in big, the team total with what the cost still due will add, and the gap to the teams around us."""
    total, estimate = s["points_total"], s["cost_estimate"]
    rank = (f"#{esc(s['rank'])}" + (f"<small>of {esc(s['teams'])}</small>" if s.get("teams") else "")
            if s["rank"] else "—")  # fmt: skip
    projected = f"{pts(total + estimate)} with the cost still due" if estimate and total is not None else ""

    def gap(side: dict[str, Any] | None, word: str, css: str) -> str:
        if not side or total is None:
            return ""
        diff = (Decimal(side["total"]) - total).copy_abs()
        return f"<div><b class={css}>{pts(diff)}</b> {word} #{esc(side['rank'])} {esc(side['team'])}</div>"

    race = gap(s.get("above"), "behind", "behind") + gap(s.get("below"), "ahead of", "ahead")
    return (
        "<div class=hero>"
        f"<div><div class=k>Rank</div><div class=rank>{rank}</div></div>"
        f"<div><div class=k>Team total</div><div class=total>{pts(total) if total is not None else '—'}</div>"
        + (f"<div class=note>{projected}</div>" if projected else "") + "</div>"
        + (f"<div><div class=race>{race}</div></div>" if race else "<div></div>")
        + "</div>"
    )  # fmt: skip


def _business(ctx, s: dict[str, Any]) -> str:
    """What a customer or a visitor wants to know: are requests answered, how fast, who books with us."""
    clients = home.customers_only(ctx)
    won_value = sum((c["won_eur"] for c in clients), Decimal(0))
    shipped = ctx.db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    median = s["median_response_s"]
    cards = _strip([
        ("Requests answered", f'{s["quoted"]} / {s["requests"]}', f'{s["missed"]} missed'),
        ("Median time to quote", f"{median} s" if median is not None else "—", "request to sent quote"),
        ("Won with customer", s["won"], f'{s["lost"]} declined' if s["lost"] else ""),
        ("Shipments booked", shipped, ""),
        ("Value won", money(won_value, "EUR") if won_value else "—", "all-in, in EUR"),
        ("Customers", len(clients), ""),
    ])  # fmt: skip
    top = sorted(clients, key=lambda c: (c["requests"], c["won"]), reverse=True)[:6]
    rows = (
        "".join(
            f'<tr data-href="{customer_href(c["customer"])}"><td>'
            + company(names.who(ctx, c["customer"]), href=customer_href(c["customer"]))
            + f"</td><td class=n>{c['requests']}</td><td class=n>{c['won']}</td>"
            f"<td class=n>{money(c['won_eur'], 'EUR') if c['won'] else '<span class=muted>—</span>'}</td></tr>"
            for c in top
        )
        or "<tr><td colspan=4 class=empty>No customer yet.</td></tr>"
    )
    table = (
        "<div class=panel><table><thead><tr><th>Customer</th><th class=n>Requests</th><th class=n>Won</th>"
        f"<th class=n>Value won</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )
    return (
        f"<div class=biz>{cards}</div>"
        + "<div class=duo><div><h2>Top customers</h2>"
        + table
        + "<div style='margin-top:8px'><a href='/crm/customers'>All customers →</a></div></div>"
        + "<div><h2>Latest activity</h2>"
        + home.feed(home.activity(ctx, 7))
        + "</div></div>"
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """`crm.quotes.stats` and the scored rounds of /v1/board/me. Shipments live in the CRM."""
    ctx = request.app.state.ctx
    s = quotes.stats(ctx)
    spend = s["spend_per_accepted_usd"]
    total = s["points_total"]
    p, accepted = s["points"], s["accepted"]
    estimate = s["cost_estimate"]
    hero = _hero(s)
    score = _strip([
        ("Accuracy", pts(p.get("accuracy", Decimal(0))), ""),
        ("Time", pts(p.get("time", Decimal(0))), ""),
        ("Cost", pts(p.get("cost", Decimal(0))), f"+ {pts(estimate)} due" if estimate else ""),
        ("Tracking", pts(p.get("tracking", Decimal(0))), ""),
        ("Human writing", pts(p.get("humanity", Decimal(0))), "" if p.get("humanity") else "none yet"),
        ("Accepted", f'{accepted} / {s["rounds"]}', f'{s["rejected"]} rejected'
         + (f' · {s["no_answer"]} no answer' if s["no_answer"] else "")),
        ("Spend / accepted", f"${spend:.4f}" if spend is not None else "—", "last hour"),
    ])  # fmt: skip
    flow = _strip([
        ("Quoted", f'{s["quoted"]} / {s["requests"]}', f'{s["fallback"]} by fallback' if s["fallback"] else ""),
        ("Missed", s["missed"], ""),
        ("Median response", f'{s["median_response_s"]} s' if s["median_response_s"] is not None else "—", ""),
        ("Won with customer", s["won"], f'{s["lost"]} lost' if s["lost"] else ""),
    ])  # fmt: skip

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
        page_head("Performance", "How fast and how well PIXEL answers, and who it works for. Updates live.")
        + "<div id=refresh data-live>"
        + CSS
        + _business(ctx, s)
        + "<h2 class=section>Challenge score</h2>"
        + f"{hero}{score}<h2>Pipeline</h2>{flow}"
        + f"<h2>Scorer, per round</h2><div class='panel rounds'><table><thead>{head}</thead>"
        f"<tbody>{score_rows or f'<tr><td colspan={cols} class=empty>No result from /v1/board/me yet.</td></tr>'}"
        f"</tbody>{foot}</table></div></div>"
    )
    return layout("Performance", body, nav="dashboard", script="live('/events',refresh());")
