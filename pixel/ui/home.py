"""The start page: what PIXEL does, in a customer's words, with the live numbers and the latest work behind it.
Links into the operator views (requests, CRM, dashboard). Reads the event log and the CRM only."""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from pixel.api.mailbox import RATEDESK, TRACKING
from pixel.core.context import Context
from pixel.crm import customers, quotes
from pixel.ui import impact, names, pipeline
from pixel.ui.page import clock, customer_href, esc, layout, money
from pixel.ui.routes import list_sessions

router = APIRouter()

DESKS = (TRACKING, RATEDESK)

CSS = """<style>
/* a soft light behind the top of the page, a faint dot grid fading out */
.glow{position:absolute;left:0;right:0;top:0;height:720px;pointer-events:none;z-index:-1;overflow:hidden}
.glow::before{content:"";position:absolute;inset:0;background:
radial-gradient(560px 340px at 18% 12%,rgba(42,91,215,.13),transparent 70%),
radial-gradient(520px 320px at 82% 18%,rgba(109,74,224,.10),transparent 70%),
radial-gradient(700px 300px at 50% 100%,rgba(42,91,215,.05),transparent 70%)}
.glow::after{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(20,22,28,.09) 1px,transparent 1px);
background-size:22px 22px;-webkit-mask-image:linear-gradient(180deg,#000,transparent 80%);
mask-image:linear-gradient(180deg,#000,transparent 80%)}
h2.big{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.15;margin:80px 0 20px}
.hero2{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:32px;align-items:center;padding:56px 0 8px}
.hero2 h1{font-size:48px;line-height:1.05;letter-spacing:-.035em;margin:0 0 18px;font-weight:500}
.hero2 h1 em{font-style:normal;background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.hero2 p.lead{font-size:16px;color:var(--ink-2);max-width:540px;margin:0 0 28px;text-wrap:pretty}
.cta{display:flex;gap:10px;flex-wrap:wrap}.cta .btn{height:40px;padding:0 18px;font-size:15px}
.clockcard{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:22px 24px;
box-shadow:var(--shadow-lg)}
.clockcard .k{font-size:12px;color:var(--muted)}
.clockcard .big{font-size:28px;font-weight:500;letter-spacing:-.025em;margin-bottom:6px;font-variant-numeric:tabular-nums;line-height:1.1}
.clockcard .row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.clockcard .row:first-of-type{border-top:0}.clockcard .row b{font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:48px}
.kpis>div{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
box-shadow:var(--shadow-sm);transition:transform .2s,box-shadow .2s}
.kpis>div:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.kpis .k{font-size:12px;color:var(--muted)}.kpis .v{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.2;
font-variant-numeric:tabular-nums;margin-top:2px}.kpis .note{font-size:12px;color:var(--muted)}
.photos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin-top:80px}
.photo{position:relative;min-height:340px;border-radius:24px;overflow:hidden;display:flex;flex-direction:column;
justify-content:flex-end;padding:28px;color:#fff;box-shadow:var(--shadow)}
.photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:transform .8s ease}
.photo:hover img{transform:scale(1.03)}
.photo::before{content:"";position:absolute;inset:0;z-index:1;background:linear-gradient(90deg,rgba(15,20,40,.72),
rgba(15,20,40,.28) 60%,rgba(15,20,40,.05)),linear-gradient(0deg,rgba(15,20,40,.45),transparent 55%)}
.photo>div{position:relative;z-index:2;max-width:380px}
.photo h2{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.15;margin:0 0 8px;color:#fff}
.photo p{margin:0;font-size:15px;line-height:1.5;color:rgba(255,255,255,.86)}
@media (max-width:820px){.photos{grid-template-columns:1fr}.photo{min-height:280px}}
.trusted{margin-top:80px;text-align:center}
.trusted h2{font-size:14px;font-weight:400;color:var(--muted);margin:0 0 20px;letter-spacing:0}
.marquee{overflow:hidden;-webkit-mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent);
mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent)}
.marquee .track{display:flex;width:max-content;animation:scroll var(--dur,40s) linear infinite}
.marquee:hover .track{animation-play-state:paused}
.marquee .set{display:flex;align-items:center;gap:56px;padding-right:56px}
.marquee a{color:var(--muted);font-size:20px;font-weight:500;letter-spacing:-.01em;white-space:nowrap;
transition:color .2s}.marquee a:hover{color:var(--ink);text-decoration:none}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.marquee .track{animation:none;width:auto}.marquee .set{flex-wrap:wrap;
justify-content:center;gap:16px 40px;padding:0}.marquee .set+.set{display:none}}
.foot{margin-top:72px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:12px;
flex-wrap:wrap}
@media (max-width:900px){.hero2{grid-template-columns:1fr}.hero2 h1{font-size:40px}
.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:420px){.kpis .v{font-size:22px}.hero2 h1{font-size:32px}}
</style>"""


def _seconds(quote) -> int | None:
    if quote is None or quote.sent_at is None:
        return None
    return max(0, round((quote.sent_at - quote.rfq.received_at).total_seconds()))


def activity(ctx: Context, limit: int = 8) -> list[dict]:
    """The latest customer threads as one line each: who, what happened, how fast."""
    found = []
    for s in list_sessions(ctx, limit=60):
        if s["customer"].lower() in DESKS or s["who"].kind != "customer":
            continue
        quote, label = s["quote"], s["status"]
        secs = _seconds(quote)
        route = f"{quote.option.origin} → {quote.option.destination}" if quote else ""
        if label in ("confirmed", "booked"):
            text = f"booked {route} with {quote.option.carrier}" if quote else "booked"
        elif label == "accepted":
            text = f"accepted the quote for {route}"
        elif label.startswith("quoted") and quote:
            text = f"quoted {money(quote.total, quote.currency)} for {route}" + (
                f" in {secs} s" if secs is not None else ""
            )
        elif label == "open":
            text = "new request, being priced now"
        elif label == "tracking":
            text = "asked where their shipment is, answered from the CRM"
        elif label == "declined":
            text = f"declined the offer for {route}" if route else "declined"
        else:
            text = label
        found.append({"who": s["who"], "text": text, "at": s["last_ts"], "id": s["id"], "css": s["css"]})
    return sorted(found, key=lambda i: i["at"] or "", reverse=True)[:limit]


def feed(items: list[dict]) -> str:
    if not items:
        return "<div class='feed panel'><div class=item><span class=muted>No request yet.</span></div></div>"
    rows = "".join(
        f"<a class=item href='/sessions/{i['id']}' style='color:inherit;text-decoration:none'>"
        f"<div class=what><span class=nm>{esc(i['who'].name)}</span> · {esc(i['text'])}</div>"
        f"<span class=t>{clock(i['at'])}</span></a>"
        for i in items
    )
    return f"<div class='feed panel' id=feed data-live>{rows}</div>"


def customers_only(ctx: Context) -> list[dict]:
    """CRM customers that are companies: no 5U desk, no carrier writing in its own thread."""
    return [c for c in customers.summary(ctx) if names.who(ctx, c["customer"]).kind == "customer"]


def numbers(ctx: Context) -> dict:
    s = quotes.stats(ctx)
    db = ctx.db
    last = db.execute("SELECT MAX(ts) FROM events WHERE kind IN ('quote.sent', 'quote.fallback')").fetchone()[0]
    shipped = db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    clients = customers_only(ctx)
    answered = s["quoted"]
    return {
        "requests": s["requests"],
        "answered": answered,
        "answered_share": f"{round(100 * answered / s['requests'])} %" if s["requests"] else "—",
        "median": s["median_response_s"],
        "missed": s["missed"],
        "shipments": shipped,
        "customers": clients,
        "last": last,
    }


PHOTOS = {"air.jpg", "road.jpg"}


@router.get("/assets/img/{name}", include_in_schema=False)
def photo(name: str) -> FileResponse:
    if name not in PHOTOS:
        raise HTTPException(404)
    return FileResponse(Path(__file__).with_name("img") / name, media_type="image/jpeg")


PHOTO_CARDS = (
    ("air.jpg", "By air, at any hour",
     "Rate sheets, the rate portal and airline bids, compared for every request."),
    ("road.jpg", "From your door",
     "When you ask for pickup, the trucker's price is part of the one all-in total."),
)  # fmt: skip


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = "", state: str = ""):
    if q or state:  # an old link to the request list
        params = {k: v for k, v in (("q", q), ("state", state)) if v}
        return RedirectResponse(f"/requests?{urlencode(params)}", 307)
    ctx: Context = request.app.state.ctx
    n = numbers(ctx)
    median = f"{n['median']} s" if n["median"] is not None else "—"
    last = datetime.fromisoformat(n["last"]) if n["last"] else None
    hero = (
        "<div class=glow aria-hidden=true></div><section class=hero2><div>"
        "<h1>Who answers at 2&nbsp;a.m.?<br><em>PIXEL does.</em></h1>"
        "<p class=lead>Send an air export request at any hour. PIXEL reads it, prices it across rate sheets, "
        "the rate portal and live carrier bids, and answers with one all-in price within minutes. "
        "It books when you accept and tells you where your shipment is.</p>"
        "<div class=cta><a class='btn primary' href='/dashboard'>Open the live dashboard</a>"
        "</div></div>"
        "<div class=clockcard><div class=k>Last answer sent</div>"
        f"<div class=big>{clock(last)[:5] if last else '—'}</div>"
        f"<div class=row><span>Requests answered</span><b>{n['answered']} / {n['requests']}</b></div>"
        f"<div class=row><span>Median time to quote</span><b>{esc(median)}</b></div>"
        f"<div class=row><span>Missed deadlines</span><b>{n['missed']}</b></div></div></section>"
    )
    kpis = (
        "<div class=kpis>"
        f"<div><div class=k>Answered</div><div class=v>{esc(n['answered_share'])}</div>"
        "<div class=note>of all requests received</div></div>"
        f"<div><div class=k>Time to quote</div><div class=v>{esc(median)}</div><div class=note>median, "
        "request to sent quote</div></div>"
        f"<div><div class=k>Shipments booked</div><div class=v>{n['shipments']}</div>"
        "<div class=note>booked by rate id or by mail</div></div>"
        f"<div><div class=k>Customers served</div><div class=v>{len(n['customers'])}</div>"
        "<div class=note>companies writing to the desk</div></div></div>"
    )
    clients = n["customers"][:20]
    links = "".join(
        f"<a href='{customer_href(c['customer'])}'>{esc(names.who(ctx, c['customer']).name)}</a>" for c in clients
    )
    # the names twice in a row, moved left by one set's width, so the loop has no seam
    trusted = (
        "<section class=trusted><h2>Who we work for</h2>"
        + (
            f"<div class=marquee style='--dur:{max(20, 4 * len(clients))}s'><div class=track>"
            f"<div class=set>{links}</div><div class=set aria-hidden=true>{links.replace('<a ', '<a tabindex=-1 ')}</div></div></div>"
            if clients
            else "<span class=muted>No customer yet.</span>"
        )
        + "</section>"
    )
    body = (
        CSS
        + hero
        + kpis
        + "<section class=photos>"
        + "".join(
            f"<article class=photo><img src='/assets/img/{f}' alt='' loading=lazy>"
            f"<div><h2>{esc(t)}</h2><p>{esc(d)}</p></div></article>"
            for f, t, d in PHOTO_CARDS
        )
        + "</section>"
        + "<h2 class=big>How it works</h2>"
        + pipeline.render(ctx)
        + trusted
        + "<h2 class=big>What PIXEL takes off your desk</h2>"
        + impact.render(ctx, n)
        + "<div class=foot><span>PIXEL · START Hack Munich 2026 · challenge by 5U AI</span>"
        + "<span>Numbers on this page are live from the event log.</span></div>"
    )
    return layout("Who answers at 2 a.m.?", body, nav="home", script="live('/events',refresh());" + pipeline.JS)
