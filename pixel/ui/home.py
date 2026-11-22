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
from pixel.ui import impact, names, pipeline, tower
from pixel.ui.page import clock, customer_href, esc, layout, money
from pixel.ui.routes import list_sessions

router = APIRouter()

DESKS = (TRACKING, RATEDESK)

CSS = """<style>
/* a soft light behind the top of the page, a faint dot grid fading out */
.glow{position:absolute;left:0;right:0;top:0;height:720px;pointer-events:none;z-index:-1;overflow:hidden}
.glow::before{content:"";position:absolute;inset:0;background:
radial-gradient(560px 340px at 18% 12%,rgba(217,95,59,.12),transparent 70%),
radial-gradient(520px 320px at 82% 18%,rgba(232,121,90,.09),transparent 70%),
radial-gradient(700px 300px at 50% 100%,rgba(217,95,59,.04),transparent 70%)}
.glow::after{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(20,22,28,.09) 1px,transparent 1px);
background-size:22px 22px;-webkit-mask-image:linear-gradient(180deg,#000,transparent 80%);
mask-image:linear-gradient(180deg,#000,transparent 80%)}
.hero2{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:32px;align-items:center;padding:56px 0 8px}
.hero2 h1{font-size:48px;line-height:1.05;letter-spacing:-.035em;margin:0 0 18px;font-weight:500}
.hero2 h1 em{font-style:normal;background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.hero2 p.lead{font-size:16px;color:var(--ink-2);max-width:540px;margin:0 0 28px;text-wrap:pretty}
.cta{display:flex;gap:10px;flex-wrap:wrap}.cta .btn{height:40px;padding:0 18px;font-size:15px}
.clockcard{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:22px 24px}
.clockcard .k{font-size:12px;color:var(--muted)}
.clockcard .big{font-size:28px;font-weight:500;letter-spacing:-.025em;margin-bottom:6px;font-variant-numeric:tabular-nums;line-height:1.1}
.clockcard .row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.clockcard .row:first-of-type{border-top:0}.clockcard .row b{font-variant-numeric:tabular-nums}
/* a section's opening: eyebrow and headline on the left, one line of context on the right */
.sec{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px 64px;align-items:end;margin:104px 0 32px}
.sec .eyebrow{font-size:12px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);
margin-bottom:12px}
.sec h2{font-size:36px;font-weight:500;letter-spacing:-.03em;line-height:1.1;margin:0}
.sec p{margin:0;justify-self:end;max-width:440px;font-size:15px;line-height:1.6;color:var(--ink-2);text-wrap:pretty}
@media (max-width:820px){.sec{grid-template-columns:1fr;margin-top:80px}.sec p{justify-self:start}.sec h2{font-size:30px}}
/* the photos: one wide, one narrow, the text on a warm shade from the bottom so they sit in the page's palette */
.photos{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:16px}
.photo{position:relative;height:440px;border-radius:20px;overflow:hidden;display:flex;flex-direction:column;
justify-content:flex-end;padding:32px;color:#fff;isolation:isolate}
.photo img{position:absolute;inset:0;z-index:-2;width:100%;height:100%;object-fit:cover;transition:transform .8s ease}
.photo:hover img{transform:scale(1.03)}
.photo::before{content:"";position:absolute;inset:0;z-index:-1;background:linear-gradient(0deg,rgba(43,38,34,.88),
rgba(43,38,34,.45) 38%,rgba(43,38,34,0) 70%),linear-gradient(0deg,rgba(217,95,59,.18),transparent 40%)}
.photo .chip{position:absolute;top:20px;left:20px;font-size:12px;font-weight:500;letter-spacing:.04em;padding:6px 12px;
border-radius:999px;color:#fff;background:rgba(43,38,34,.35);border:1px solid rgba(255,255,255,.22);
-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
.photo h3{font-size:28px;font-weight:500;letter-spacing:-.025em;line-height:1.15;margin:0 0 8px;color:#fff;max-width:420px}
.photo p{margin:0;font-size:15px;line-height:1.5;color:rgba(255,255,255,.82);max-width:420px}
@media (max-width:820px){.photos{grid-template-columns:1fr}.photo{height:320px;padding:24px}}
.trusted{margin-top:96px;text-align:center}
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
/* the FAQ: one headline, the questions as cards below it; one open at a time */
.faq{margin-top:112px}
.faq h2{font-size:36px;font-weight:500;letter-spacing:-.03em;line-height:1.1;margin:0 0 32px;text-align:center}
.faq .qs{display:grid;gap:10px;max-width:880px;margin:0 auto}
.faq details{background:var(--surface);border:1px solid var(--line);border-radius:16px;transition:border-color .2s,
box-shadow .2s}
.faq details:hover{border-color:var(--line-2)}
.faq details[open]{border-color:var(--line-2);box-shadow:0 1px 2px rgba(20,22,28,.04),0 8px 24px rgba(20,22,28,.05)}
.faq summary{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:20px 24px;cursor:pointer;
list-style:none;font-size:16px;font-weight:500;letter-spacing:-.01em}
.faq summary::-webkit-details-marker{display:none}
.faq summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:16px}
.faq summary i{position:relative;flex:none;width:28px;height:28px;border-radius:50%;background:var(--sunk);
transition:background .2s}
.faq summary i::before,.faq summary i::after{content:"";position:absolute;left:50%;top:50%;width:11px;height:1.5px;
margin:-.75px 0 0 -5.5px;background:var(--ink);border-radius:1px;transition:transform .3s,background .2s}
.faq summary i::after{transform:rotate(90deg)}
.faq details[open] summary i{background:var(--accent)}
.faq details[open] summary i::before,.faq details[open] summary i::after{background:#fff}
.faq details[open] summary i::after{transform:rotate(0)}
.faq details p{margin:0;padding:0 24px 22px;font-size:15px;line-height:1.6;color:var(--ink-2);max-width:680px;
text-wrap:pretty}
/* contact: a closing call to action, a dark warm panel with the mail you would send on the right */
.contact{position:relative;isolation:isolate;overflow:hidden;margin-top:120px;padding:72px 64px 0;border-radius:28px;
background:#1c1916;color:#f4efe9;box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 30px 80px -40px rgba(43,38,34,.55)}
.contact::before{content:"";position:absolute;inset:0;z-index:-1;background:
radial-gradient(620px 380px at 88% 8%,rgba(217,95,59,.34),transparent 65%),
radial-gradient(520px 320px at 0% 100%,rgba(232,121,90,.14),transparent 70%)}
.contact::after{content:"";position:absolute;inset:0;z-index:-1;background-image:radial-gradient(rgba(255,255,255,.07) 1px,
transparent 1px);background-size:22px 22px;-webkit-mask-image:linear-gradient(180deg,transparent,#000 30%,transparent);
mask-image:linear-gradient(180deg,transparent,#000 30%,transparent)}
.contact .grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:56px;align-items:center}
.contact h2{font-size:56px;font-weight:500;letter-spacing:-.04em;line-height:1.02;margin:0 0 20px;color:#fff;max-width:560px;
text-wrap:balance}
.contact h2 em{font-style:normal;background:linear-gradient(90deg,#f08a66,#ffc3ab);-webkit-background-clip:text;
background-clip:text;color:transparent}
.contact p.lead{margin:0 0 32px;max-width:460px;font-size:16px;line-height:1.6;color:rgba(244,239,233,.7);text-wrap:pretty}
.contact .cta{display:flex;gap:12px;flex-wrap:wrap}
.contact .btn{height:48px;padding:0 24px;font-size:15px;font-weight:500;border-radius:999px;gap:10px;
transition:transform .2s,background .2s,box-shadow .2s}
.contact .btn.primary{box-shadow:0 10px 30px -10px rgba(217,95,59,.8)}
.contact .btn.primary:hover{opacity:1;transform:translateY(-1px);background:#e46a44}
.contact .btn.ghost{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.16);color:#f4efe9}
.contact .btn.ghost:hover{background:rgba(255,255,255,.12);text-decoration:none}
.contact .btn span{transition:transform .2s}.contact .btn:hover span{transform:translateX(3px)}
/* the mail card: what a request looks like, and the answer it gets */
.mail{position:relative;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:20px;
padding:8px;-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);transform:rotate(1.2deg);
box-shadow:0 40px 80px -30px rgba(0,0,0,.6)}
.mail .in{background:#fbf8f5;color:var(--ink);border-radius:14px;padding:20px 22px 56px;font-size:14px;line-height:1.55}
.mail .hd{display:grid;grid-template-columns:56px 1fr;gap:6px 8px;padding-bottom:14px;margin-bottom:14px;
border-bottom:1px solid var(--line);font-size:13px}
.mail .hd span{color:var(--muted)}.mail .hd b{font-weight:500}
.mail .body{color:var(--ink-2)}
.mail .reply{position:absolute;left:-28px;bottom:-22px;display:flex;align-items:center;gap:12px;padding:12px 16px 12px 12px;
background:#fff;color:var(--ink);border-radius:14px;transform:rotate(-2.4deg);font-size:13px;
box-shadow:0 20px 40px -16px rgba(0,0,0,.5)}
.mail .reply i{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;background:var(--accent);color:#fff;
font-style:normal;font-weight:700}
.mail .reply small{display:block;color:var(--muted);font-size:12px}
/* three facts along the bottom edge of the panel */
.contact .facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:72px -64px 0;
border-top:1px solid rgba(255,255,255,.09)}
.contact .facts div{padding:24px 32px 28px;border-left:1px solid rgba(255,255,255,.09)}
.contact .facts div:first-child{border-left:0;padding-left:64px}
.contact .facts b{display:block;font-size:26px;font-weight:500;letter-spacing:-.025em;color:#fff;
font-variant-numeric:tabular-nums}
.contact .facts span{font-size:13px;color:rgba(244,239,233,.55)}
@media (max-width:900px){.faq,.contact{margin-top:80px}.faq h2{font-size:30px}
.contact{padding:48px 24px 0;border-radius:22px}.contact .grid{grid-template-columns:1fr;gap:48px}
.contact h2{font-size:38px}.mail{transform:none;margin:0 8px 0 20px}.mail .reply{left:-16px}
.contact .facts{grid-template-columns:1fr;margin:56px -24px 0}
.contact .facts div,.contact .facts div:first-child{border-left:0;border-top:1px solid rgba(255,255,255,.09);
padding:18px 24px}.contact .facts div:first-child{border-top:0}}
@media (prefers-reduced-motion:reduce){.contact *{transition:none!important}}
@media (prefers-reduced-motion:reduce){.faq *{transition:none!important}}
/* the wordmark at the very bottom, big and shown in full */
.bigmark{--fs:min(30vw,420px);position:relative;width:100vw;margin:56px calc(50% - 50vw) 0;
height:calc(var(--fs) * .8);overflow:hidden;pointer-events:none;user-select:none}
.bigmark span{position:absolute;left:50%;top:0;transform:translateX(-50%);display:flex;align-items:center;gap:.06em;
font:900 var(--fs)/.74 Satoshi,Jost,system-ui,sans-serif;letter-spacing:-.04em;color:var(--ink);white-space:nowrap}
.bigmark i{display:block;width:.36em;height:.36em;background:#DE5531;flex:none}
.foot{margin-top:72px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:12px;
flex-wrap:wrap}
@media (max-width:900px){.hero2{grid-template-columns:1fr}.hero2 h1{font-size:40px}}
@media (max-width:420px){.hero2 h1{font-size:32px}}
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
    ("air.jpg", "Air freight", "By air, at any hour",
     "Rate sheets, the rate portal and airline bids, compared for every request."),
    ("road.jpg", "Pickup", "From your door",
     "When you ask for pickup, the trucker's price is part of the one all-in total."),
)  # fmt: skip


def _section(eyebrow: str, title: str, lead: str) -> str:
    return (
        f"<header class=sec><div><div class=eyebrow>{esc(eyebrow)}</div><h2>{esc(title)}</h2></div>"
        f"<p>{esc(lead)}</p></header>"
    )


FAQ = (
    ("What kind of shipments does PIXEL handle?",
     ("Air export requests: airport to airport, or from your door when you ask for pickup. Send weight, dimensions, "
     "origin and destination the way you always do, in your own words and with your own attachments.")),
    ("How fast do I get a price?",
     "Within minutes, at any hour. The median time from your mail to our quote is shown live at the top of this page."),
    ("Where do the prices come from?",
     ("Every rate sheet, the rate portal and live bids from the airlines, compared to the cent. Code does the "
     "arithmetic, not a language model, and the price you get is the price we buy at.")),
    ("What if my request is missing something?",
     ("If a fact the price depends on is missing, PIXEL asks you one short question in the same thread and quotes as "
     "soon as you answer. Everything else it reads from your mail.")),
    ("Can a person step in?",
     "Yes. Every request is open to the team in real time, and anyone can take one over, change it or answer by hand."),
    ("How do I book, and how do I track it?",
     ("Reply that you accept. PIXEL books the shipment, sends you the booking reference and answers \"where is my "
     "shipment?\" from its own records, day and night.")),
)  # fmt: skip


CONTACT = "team@hackloop.mail"  # our mailbox: where a forwarder writes to PIXEL

FAQ_JS = """
document.querySelectorAll('.faq details').forEach(d=>d.addEventListener('toggle',()=>{if(!d.open)return;
document.querySelectorAll('.faq details[open]').forEach(o=>{if(o!==d)o.open=false;});}));
"""


def faq() -> str:
    items = "".join(
        f"<details name=faq{' open' if k == 0 else ''}><summary>{esc(q)}<i aria-hidden=true></i></summary>"
        f"<p>{esc(a)}</p></details>"
        for k, (q, a) in enumerate(FAQ)
    )
    return f"<section class=faq><h2>Frequently asked questions</h2><div class=qs>{items}</div></section>"


def contact(n: dict) -> str:
    median = f"{n['median']} s" if n["median"] is not None else "Minutes"
    fast = f"in {median}" if n["median"] is not None else "within minutes"
    return (
        "<section class=contact><div class=grid><div>"
        "<h2>Have a shipment to move <em>tonight?</em></h2>"
        "<p class=lead>Send your request to PIXEL the way you always write it. One all-in price comes back within "
        "minutes, at any hour, and a person on the team is always one reply away.</p><div class=cta>"
        f"<a class='btn primary' href='mailto:{esc(CONTACT)}'>Email {esc(CONTACT)} <span aria-hidden=true>→</span></a>"
        "<a class='btn ghost' href='/requests'>See live requests</a></div></div>"
        "<div class=mail aria-hidden=true><div class=in>"
        f"<div class=hd><span>To</span><b>{esc(CONTACT)}</b><span>Subject</span><b>MUC → JFK, 2 pallets tonight</b></div>"
        "<div class=body>Hi team, two pallets, 480&nbsp;kg, 120×80×110&nbsp;cm each. Pickup from our warehouse in "
        "Garching, ready at 18:00. Can you get it out tonight?</div></div>"
        f"<div class=reply><i>P</i><div><b>Quote sent</b><small>All-in price · {esc(fast)}</small></div></div>"
        "</div></div>"
        "<div class=facts>"
        f"<div><b>{esc(median)}</b><span>Median time to quote</span></div>"
        f"<div><b>{n['answered_share']}</b><span>Requests answered</span></div>"
        "<div><b>24 / 7</b><span>Weekends and 2&nbsp;a.m. included</span></div>"
        "</div></section>"
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = "", state: str = ""):
    if q or state:  # an old link to the request list
        params = {k: v for k, v in (("q", q), ("state", state)) if v}
        return RedirectResponse(f"/requests?{urlencode(params)}", 307)
    return start_page(request.app.state.ctx, with_map=True)


@router.get("/classic", response_class=HTMLResponse)
async def classic(request: Request):
    """The start page as it was before the world map: headline, lead text and the clock card."""
    return start_page(request.app.state.ctx, with_map=False)


def start_page(ctx: Context, *, with_map: bool) -> HTMLResponse:
    n = numbers(ctx)
    median = f"{n['median']} s" if n["median"] is not None else "—"
    last = datetime.fromisoformat(n["last"]) if n["last"] else None
    hero = (
        tower.render(ctx)
        if with_map
        else (
            "<div class=glow aria-hidden=true></div><section class=hero2><div>"
            "<h1>Who answers at 2&nbsp;a.m.?<br><em>PIXEL does.</em></h1>"
            "<p class=lead>Send an air export request at any hour. PIXEL reads it, prices it across rate sheets, "
            "the rate portal and live carrier bids, and answers with one all-in price within minutes. "
            "It books when you accept and tells you where your shipment is.</p>"
            "</div>"
            "<div class=clockcard><div class=k>Last answer sent</div>"
            f"<div class=big>{clock(last)[:5] if last else '—'}</div>"
            f"<div class=row><span>Requests answered</span><b>{n['answered']} / {n['requests']}</b></div>"
            f"<div class=row><span>Median time to quote</span><b>{esc(median)}</b></div>"
            f"<div class=row><span>Missed deadlines</span><b>{n['missed']}</b></div></div></section>"
        )
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
        + _section("What we ship", "Air export, from your door to the runway",
                   "One mail in, one price back: freight, fuel, security and pickup in a single all-in total.")
        + "<section class=photos>"
        + "".join(
            f"<article class=photo><img src='/assets/img/{f}' alt='' loading=lazy><span class=chip>{esc(c)}</span>"
            f"<h3>{esc(t)}</h3><p>{esc(d)}</p></article>"
            for f, c, t, d in PHOTO_CARDS
        )
        + "</section>"
        + pipeline.render(ctx)
        + trusted
        + _section("Use cases", "What PIXEL takes off your desk",
                   "Three jobs a forwarder's team does by hand every day, and the numbers since PIXEL took them over.")
        + impact.render(ctx, n)
        + faq()
        + contact(n)
        + "<div class=foot><span>PIXEL · START Hack Munich 2026 · challenge by 5U AI</span>"
        + "<span>Numbers on this page are live from the event log.</span></div>"
        + "<div class=bigmark aria-hidden=true><span><i></i>PIXEL</span></div>"
    )  # fmt: skip
    return HTMLResponse(
        layout(
            "Nobody likes doing logistics" if with_map else "Who answers at 2 a.m.?",
            body,
            nav="home",
            script="live('/events',refresh());" + pipeline.JS + FAQ_JS,
        )
    )
