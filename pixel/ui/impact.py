"""The start page's use cases: for each job PIXEL takes off a forwarder's desk, the problem, what PIXEL does about it,
and three figures from the event log that show it. One row per job, one below the other."""

from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from pixel.core.context import Context
from pixel.ui.page import esc

OFFICE = ZoneInfo("Europe/Berlin")
OPEN, CLOSE = 8, 18  # office hours, local time

CSS = """<style>
/* the three jobs as rows: title, problem and answer as text, the figures as a column on the right */
.uses{border-top:1px solid var(--line-2)}
.use{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,5fr) minmax(0,3fr);gap:48px;padding:44px 0;
border-bottom:1px solid var(--line-2);align-items:start}
.use .idx{font-size:13px;font-weight:500;color:var(--accent);font-variant-numeric:tabular-nums;letter-spacing:.04em;
margin-bottom:12px}
.use h3{font-size:30px;font-weight:500;letter-spacing:-.03em;line-height:1.1;margin:0}
.use .ps{display:grid;gap:22px}
.use .p,.use .s{padding-left:18px;font-size:15px;line-height:1.6;text-wrap:pretty}
.use .p{border-left:2px solid var(--line-2);color:var(--muted)}
.use .s{border-left:2px solid var(--accent);color:var(--ink)}
.use .lbl{font-size:12px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px}
.use .p .lbl{color:var(--muted)}.use .s .lbl{color:var(--accent)}
.use .figs{display:grid}
.use .figs>div{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:12px 0;
border-top:1px solid var(--line)}
.use .figs>div:first-child{border-top:0;padding-top:0}
.use .figs b{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.1;color:var(--ink);
font-variant-numeric:tabular-nums;white-space:nowrap}
.use .figs>div:first-child b{color:var(--accent)}
.use .figs span{font-size:13px;color:var(--muted);text-align:right}
@media (max-width:980px){.use{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:28px 40px}
.use>header{grid-column:1/-1}}
@media (max-width:640px){.use{grid-template-columns:1fr;padding:32px 0}.use h3{font-size:24px}}
</style>"""


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def figures(ctx: Context, n: dict) -> dict:
    """Tracking answers and their speed, quotes sent outside office hours, the last answer."""
    db = ctx.db
    rows = db.execute(
        "SELECT a.ts AS answered, (SELECT MIN(ts) FROM events b WHERE b.session_id = a.session_id) AS asked"
        " FROM events a WHERE a.kind = 'tracking.answered'"
    ).fetchall()
    waits = [(_ts(r["answered"]) - _ts(r["asked"])).total_seconds() for r in rows if r["answered"] and r["asked"]]
    sent = [_ts(r[0]) for r in db.execute("SELECT ts FROM events WHERE kind IN ('quote.sent', 'quote.fallback')")]
    after = sum(1 for t in sent if t and not OPEN <= t.astimezone(OFFICE).hour < CLOSE)
    last = _ts(n["last"])
    return {
        "tracked": len(rows),
        "track_wait": f"{round(median(waits))} s" if waits else "—",
        "after_hours": after,
        "last": last.astimezone(OFFICE).strftime("%H:%M") if last else "—",
    }


def _row(index: int, title: str, figs: list[tuple[str, str]], problem: str, solution: str) -> str:
    cells = "".join(f"<div><b>{esc(v)}</b><span>{esc(k)}</span></div>" for v, k in figs)
    return (
        f"<article class=use><header><div class=idx>{index:02d}</div><h3>{esc(title)}</h3></header><div class=ps>"
        f"<div class=p><div class=lbl>The problem</div>{esc(problem)}</div>"
        f"<div class=s><div class=lbl>With PIXEL</div>{esc(solution)}</div></div>"
        f"<div class=figs>{cells}</div></article>"
    )


def render(ctx: Context, n: dict) -> str:
    f = figures(ctx, n)
    median_quote = f"{n['median']} s" if n["median"] is not None else "—"
    return CSS + (
        "<div class=uses>"
        + _row(
            1,
            "Rate management",
            [
                (median_quote, "Time to quote"),
                (n["answered_share"], "Requests answered"),
                (str(n["answered"]), "Quotes sent"),
            ],
            "Every request needs rate sheets, the portal and carrier bids checked. By hand that takes long enough to "
            "lose the deal.",
            "PIXEL reads the request, compares every price source to the cent and sends one all-in price in the "
            "customer's own thread.",
        )
        + _row(
            2,
            "Track & trace",
            [
                (str(f["tracked"]), "Questions answered"),
                (f["track_wait"], "Time to answer"),
                (str(n["shipments"]), "Shipments booked"),
            ],
            "Customers ask where their shipment is, and someone has to dig through mails and bookings to tell them.",
            "PIXEL answers from its own CRM: status, airline and booking reference, in the customer's tone.",
        )
        + _row(
            3,
            "Customer service",
            [
                (str(f["after_hours"]), "Quotes sent after hours"),
                (str(n["missed"]), "Requests missed"),
                (f["last"], "Last answer sent"),
            ],
            "A request that lands at 2 a.m. waits until the office opens. By then the customer has booked elsewhere.",
            "PIXEL answers within minutes, day and night, and a person can step into any request at any time.",
        )
        + "</div>"
    )
