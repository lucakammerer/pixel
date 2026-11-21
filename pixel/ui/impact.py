"""The start page's use cases: for each job PIXEL takes off a forwarder's desk, the problem, what PIXEL does about it,
and three figures from the event log that show it."""

from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from pixel.core.context import Context
from pixel.ui.page import esc

OFFICE = ZoneInfo("Europe/Berlin")
OPEN, CLOSE = 8, 18  # office hours, local time

CSS = """<style>
.uses{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.use{background:var(--sunk);border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:var(--shadow-sm)}
.use.wide{grid-column:1/-1}
.use .figs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;background:var(--surface);
padding:18px 20px;border-bottom:1px solid var(--line);text-align:center}
.use .figs b{display:block;font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.15;color:var(--navy);
font-variant-numeric:tabular-nums}
.use .figs span{font-size:12px;color:var(--ink-2)}
.use .body{padding:24px 24px 26px}
.use h3{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.15;margin:0 0 18px}
.use .ps{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.use .p,.use .s{border-radius:12px;padding:16px 16px 18px;font-size:13px;line-height:1.5}
.use .p{background:var(--surface);border:1px solid var(--line);color:var(--ink-2)}
.use .s{background:var(--navy);color:#e8ecf6;box-shadow:0 10px 24px -12px rgba(39,52,86,.6)}
.use .p h4,.use .s h4{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:500;margin:0 0 8px}
.use .p h4{color:var(--ink)}.use .s h4{color:#fff}
.use h4 svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;
stroke-linejoin:round}
.use.wide .ps{max-width:none}
@media (max-width:900px){.uses{grid-template-columns:1fr}}
@media (max-width:520px){.use .ps{grid-template-columns:1fr}.use .figs b{font-size:20px}.use h3{font-size:20px}}
</style>"""

PROBLEM = (
    '<svg viewBox="0 0 24 24" aria-hidden=true><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/></svg>'
)
SOLUTION = (
    '<svg viewBox="0 0 24 24" aria-hidden=true><circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 9.8"/></svg>'
)


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


def _card(title: str, figs: list[tuple[str, str]], problem: str, solution: str, *, wide: bool = False) -> str:
    cells = "".join(f"<div><b>{esc(v)}</b><span>{esc(k)}</span></div>" for v, k in figs)
    return (
        f"<article class='use{' wide' if wide else ''}'><div class=figs>{cells}</div><div class=body>"
        f"<h3>{esc(title)}</h3><div class=ps>"
        f"<div class=p><h4>{PROBLEM}Problem</h4>{esc(problem)}</div>"
        f"<div class=s><h4>{SOLUTION}Solution</h4>{esc(solution)}</div></div></div></article>"
    )


def render(ctx: Context, n: dict) -> str:
    f = figures(ctx, n)
    median_quote = f"{n['median']} s" if n["median"] is not None else "—"
    return CSS + (
        "<div class=uses>"
        + _card(
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
        + _card(
            "Track & trace",
            [
                (str(f["tracked"]), "Questions answered"),
                (f["track_wait"], "Time to answer"),
                (str(n["shipments"]), "Shipments booked"),
            ],
            "Customers ask where their shipment is, and someone has to dig through mails and bookings to tell them.",
            "PIXEL answers from its own CRM: status, airline and booking reference, in the customer's tone.",
        )
        + _card(
            "Customer service",
            [
                (str(f["after_hours"]), "Quotes sent outside office hours"),
                (str(n["missed"]), "Requests missed"),
                (f["last"], "Last answer sent"),
            ],
            "A request that lands at 2 a.m. waits until the office opens. By then the customer has booked elsewhere.",
            "PIXEL answers within minutes, day and night, and a person can step into any request at any time.",
            wide=True,
        )
        + "</div>"
    )
