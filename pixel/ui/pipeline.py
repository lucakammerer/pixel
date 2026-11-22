"""The start page's "How it works": one real request travels the pipeline, one large figure per stop, on a loop.

The example is the newest booked request in the CRM (else the newest quote), so every figure on it ran here."""

from datetime import datetime
from typing import Any

from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.ui import names
from pixel.ui.page import esc, money

CSS = """<style>
/* one statement on top, then one large figure at a time over a five-stop timeline. The accent marks only the
price, the moment the section is about; everything else is white at three strengths */
.pipe{margin-top:88px;padding:60px 48px 44px;border-radius:28px;background:var(--navy);color:#fff;text-align:center}
.pipe .ey{margin:0 0 10px;font-size:15px;color:rgba(255,255,255,.55)}
.pipe h2{margin:0;font-size:40px;font-weight:500;letter-spacing:-.035em;line-height:1.05;color:#fff}
.pipe h2 span{color:rgba(255,255,255,.45)}
.pipe .scenes{position:relative;height:150px;margin-top:40px}
.pipe .sc{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
opacity:0;transform:translateY(12px) scale(.98);transition:opacity .6s ease,transform .8s cubic-bezier(.2,.7,.2,1)}
.pipe .sc.on{opacity:1;transform:none}
.pipe .sc .v{max-width:100%;font-size:clamp(32px,4.6vw,54px);font-weight:500;letter-spacing:-.045em;line-height:1.02;
font-variant-numeric:tabular-nums;text-wrap:balance;overflow-wrap:anywhere}
.pipe .sc.hero .v{color:var(--accent-2)}
.pipe .sc .c{margin-top:12px;font-size:17px;line-height:1.4;color:rgba(255,255,255,.6);text-wrap:balance}
.pipe .tl{position:relative;max-width:820px;margin:32px auto 0}
.pipe .tl-line{position:absolute;top:39px;left:10%;right:10%;height:2px;border-radius:2px;background:rgba(255,255,255,.12)}
.pipe .tl-line i{position:absolute;inset:0 auto 0 0;width:0;border-radius:2px;background:#fff;
transition:width .9s cubic-bezier(.6,0,.2,1)}
.pipe .stps{position:relative;display:grid;grid-template-columns:repeat(5,minmax(0,1fr))}
.pipe .stp{display:flex;flex-direction:column;align-items:center;padding:0;border:0;background:none;color:inherit;
font:inherit;cursor:pointer}
.pipe .stp .t{order:-1;margin-bottom:14px;font-size:13px;line-height:20px;white-space:nowrap;
font-variant-numeric:tabular-nums;color:rgba(255,255,255,.35);transition:color .4s}
.pipe .stp i{width:12px;height:12px;border-radius:50%;background:var(--navy);
box-shadow:inset 0 0 0 2px rgba(255,255,255,.25);transition:background .4s,box-shadow .4s}
.pipe .stp b{margin-top:16px;font-size:15px;font-weight:500;color:rgba(255,255,255,.4);transition:color .4s}
.pipe .stp:focus-visible{outline:2px solid var(--accent-2);outline-offset:6px;border-radius:8px}
.pipe .stp.done i{background:#fff;box-shadow:none}
.pipe .stp.done b,.pipe .stp.done .t{color:rgba(255,255,255,.75)}
.pipe .stp.now i{background:#fff;box-shadow:0 0 0 6px rgba(255,255,255,.14)}
.pipe .stp.now b,.pipe .stp.now .t{color:#fff}
.pipe .stp.hero.now i,.pipe .stp.hero.done i{background:var(--accent-2)}
.pipe .stp.hero.now i{box-shadow:0 0 0 6px rgba(232,121,90,.22)}
.pipe .proof{margin:32px 0 0;font-size:13px;color:rgba(255,255,255,.4)}
@media (max-width:1000px){.pipe{margin-top:72px;padding:52px 32px 40px}.pipe h2{font-size:34px}}
@media (max-width:560px){.pipe{padding:44px 16px 36px}.pipe h2{font-size:28px}.pipe .scenes{height:150px;margin-top:32px}
.pipe .sc .c{font-size:15px}.pipe .stp .t{font-size:11px}.pipe .stp b{font-size:12px}}
@media (prefers-reduced-motion:reduce){.pipe *{transition:none!important}}
</style>"""

JS = """
(()=>{const p=document.getElementById('pipe');if(!p)return;
const sc=[...p.querySelectorAll('.sc')],st=[...p.querySelectorAll('.stp')],fill=p.querySelector('.tl-line i'),
hero=st.findIndex(s=>s.classList.contains('hero'));let i=0,tm=0;
function show(k,go){i=k;clearTimeout(tm);
sc.forEach((s,n)=>s.classList.toggle('on',n===k));
st.forEach((s,n)=>{s.classList.toggle('done',n<k);s.classList.toggle('now',n===k);});
fill.style.width=(k/(st.length-1)*100)+'%';
if(go)tm=setTimeout(()=>show((k+1)%st.length,true),k===hero?4200:k===st.length-1?3000:2400);}
const still=matchMedia('(prefers-reduced-motion:reduce)').matches;
st.forEach((s,n)=>s.addEventListener('click',()=>show(n,!still)));
if(still){show(hero,false);return;}
show(0,false);
new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)show(0,true);else clearTimeout(tm);}),
{threshold:.4}).observe(p);})();
"""


def span(seconds: float) -> str:
    """Elapsed time as people read it: 42 s, 3 min 05 s, 2 h 14 min."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def _first(ctx: Context, session_id: int, kinds: tuple[str, ...], last: bool = False) -> datetime | None:
    marks = ",".join("?" * len(kinds))
    row = ctx.db.execute(
        f"SELECT {'MAX' if last else 'MIN'}(ts) FROM events WHERE session_id = ? AND kind IN ({marks})",
        (session_id, *kinds),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def example(ctx: Context) -> dict[str, Any] | None:
    """The newest booked request (else the newest quote sent) with what each station did for it."""
    shipment = next(iter(shipments.recent(ctx, 1)), None)
    quote = quotes.for_session(ctx, shipment.session_id) if shipment else None
    if quote is None:
        row = ctx.db.execute(
            "SELECT session_id FROM events WHERE kind IN ('quote.sent','quote.fallback') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        quote = quotes.for_session(ctx, row[0]) if row else None
        shipment = None
    if quote is None or quote.sent_at is None:
        return None
    options = ctx.db.execute(
        "SELECT COUNT(*) FROM events WHERE session_id = ? AND kind = 'option.found'", (quote.session_id,)
    ).fetchone()[0]
    o, r = quote.option, quote.rfq
    start = r.received_at
    sent = (quote.sent_at - start).total_seconds()
    # when each station finished for this request, from its own events; a missing one falls back to its neighbour
    read = _first(ctx, quote.session_id, ("rfq.extracted",))
    priced = _first(ctx, quote.session_id, ("option.found", "quote.drafted"), last=True)
    read_s = min(sent, (read - start).total_seconds()) if read else 0.0
    priced_s = min(sent, max(read_s, (priced - start).total_seconds())) if priced else sent
    booking = bookings.for_session(ctx, quote.session_id) if shipment else None
    clock = [span(0), span(read_s), span(priced_s), span(sent)]
    clock.append(span((booking.created_at - start).total_seconds()) if booking else "not booked yet")
    return {
        "company": names.who(ctx, r.customer).name,
        "weight": f"{r.weight_kg} kg",
        "lane": f"{o.origin} → {o.destination}",
        "options": options,
        "carrier": o.carrier,
        "total": money(quote.total, quote.currency),
        "secs": round(sent),
        "clock": clock,
        "booked": bool(shipment),
        "ref": (shipment.booking_reference or shipment.id) if shipment else "",
    }


def render(ctx: Context) -> str:
    ex = example(ctx)
    if ex:
        n = ex["options"]
        scenes = (  # (big figure, one sentence) per stop, all from this one request
            (esc(ex["company"]), "emails asking for a price."),
            (f"{esc(ex['weight'])} · {esc(ex['lane'])}", f"Weight, lane and dates, read in {esc(ex['clock'][1])}."),
            (f"{n} option{'s' if n != 1 else ''}" if n else "Every option",
             f"Compared to the cent. {esc(ex['carrier'])} wins."),
            (esc(ex["total"]), f"All-in, in their inbox {esc(span(ex['secs']))} after they asked."),
            ("Booked.", f"{esc(ex['ref'])}, tracked until it lands.") if ex["booked"]
            else ("On their yes", "Booked by rate id, then tracked until it lands."),
        )  # fmt: skip
        times = [esc(t) for t in ex["clock"][:4]] + [esc(ex["clock"][4]) if ex["booked"] else "on yes"]
        proof = "A real request, replayed. Every figure on it was computed here."
    else:
        scenes = (("One email", "A customer asks for a price."), ("Weight, lane", "Pickup and dates, read in seconds."),
                  ("Every source", "Rate sheets, the portal and carrier bids, compared."),
                  ("All-in", "One price, in their own thread."), ("Booked.", "And tracked until it lands."))  # fmt: skip
        times = ["&nbsp;"] * 5
        proof = "Every request takes this path, day and night."
    titles = ("Request", "Read", "Price", "Reply", "Book")
    hero = 3  # the reply: the one moment in the accent colour
    shown = "".join(
        f"<div class='sc{' hero' if k == hero else ''}'><div class=v>{v}</div><div class=c>{c}</div></div>"
        for k, (v, c) in enumerate(scenes)
    )
    stops = "".join(
        f"<button type=button class='stp{' hero' if k == hero else ''}'><span class=t>{t}</span><i></i>"
        f"<b>{esc(title)}</b></button>"
        for k, (title, t) in enumerate(zip(titles, times, strict=True))
    )
    return CSS + (
        "<section class=pipe id=pipe><p class=ey>How it works</p>"
        "<h2>One email in.<br><span>One all-in price out.</span></h2>"
        f"<div class=scenes>{shown}</div>"
        f"<div class=tl><div class=tl-line><i></i></div><div class=stps>{stops}</div></div>"
        f"<p class=proof>{proof}</p></section>"
    )
