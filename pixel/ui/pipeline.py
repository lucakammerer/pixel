"""The start page's "How it works": one real request travels the pipeline, station by station, on a loop.

The example is the newest booked request in the CRM (else the newest quote), so every figure on it ran here."""

from typing import Any

from pixel.core.context import Context
from pixel.crm import quotes, shipments
from pixel.ui import names
from pixel.ui.page import esc, money

ICONS = {  # 24x24 line icons, stroke only
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/>',
    "read": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 12h6M10 16h6"/>',
    "price": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "send": '<path d="M21 3 10 14M21 3l-7 18-4-7-7-4z"/>',
    "book": '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="m3 7 9 4 9-4M12 11v10"/>',
}

CSS = """<style>
.pipe{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:24px 24px 18px;
overflow:hidden;box-shadow:var(--shadow)}
.pipe .stations{position:relative;display:grid;gap:22px}
.pipe .rail{position:absolute;width:2px;background:var(--line-2);border-radius:2px}
.pipe .fill{position:absolute;left:0;top:0;width:100%;height:0;background:var(--accent);border-radius:2px;
transition:height .9s cubic-bezier(.4,0,.2,1)}
.pipe .dot{position:absolute;left:50%;top:0;width:12px;height:12px;margin:-6px 0 0 -6px;border-radius:50%;
background:var(--accent);box-shadow:0 0 0 5px color-mix(in srgb,var(--accent) 22%,transparent);
transition:top .9s cubic-bezier(.4,0,.2,1)}
.st5{display:grid;grid-template-columns:52px minmax(0,1fr);column-gap:18px;align-items:center;min-width:0}
.st5 .ic{grid-row:span 2;width:52px;height:52px;border-radius:50%;display:grid;place-items:center;
background:var(--surface);border:1.5px solid var(--line-2);color:var(--muted);transition:all .45s ease;position:relative;
z-index:1}
.st5 .ic svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;
stroke-linejoin:round}
.st5 h3{font-size:14px;font-weight:500;margin:0;color:var(--ink-2);transition:color .45s}
.st5 .ex{justify-self:start;margin-top:6px;font-size:13px;line-height:1.45;padding:6px 10px;border-radius:8px;
background:var(--sunk);color:var(--ink);opacity:0;transform:translateX(-6px);transition:opacity .45s,transform .45s;
font-variant-numeric:tabular-nums}
.st5.done .ic{border-color:var(--accent);color:var(--accent)}
.st5.now .ic{background:var(--accent);border-color:var(--accent);color:#fff;transform:scale(1.08);
box-shadow:0 0 0 8px color-mix(in srgb,var(--accent) 16%,transparent)}
.st5.done h3,.st5.now h3{color:var(--ink)}
.st5.done .ex,.st5.now .ex{opacity:1;transform:none}
.pipe .cap{margin-top:22px;padding-top:14px;border-top:1px solid var(--line);display:flex;justify-content:space-between;
gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.pipe .cap b{color:var(--ink);font-weight:500}
.pipe .clock{font-variant-numeric:tabular-nums}
@media (max-width:420px){.pipe{padding:18px 16px 14px}}
@media (prefers-reduced-motion:reduce){.pipe *{transition:none!important}}
</style>"""

JS = """
(()=>{const p=document.getElementById('pipe');if(!p)return;let i=-1;
const st=[...p.querySelectorAll('.st5')],fill=p.querySelector('.fill'),dot=p.querySelector('.dot'),
clk=p.querySelector('.clock'),secs=JSON.parse(p.dataset.secs),rail=p.querySelector('.rail'),
box=p.querySelector('.stations');
function place(){const b=box.getBoundingClientRect(),ic=p.querySelectorAll('.ic'),
a=ic[0].getBoundingClientRect(),z=ic[ic.length-1].getBoundingClientRect();
Object.assign(rail.style,{left:a.left+a.width/2-b.left-1+'px',top:a.top+a.height/2-b.top+'px',
height:(z.top-a.top)+'px'});}
function show(i){st.forEach((s,k)=>{s.classList.toggle('done',k<i);s.classList.toggle('now',k===i);});
const f=(100*Math.max(0,i)/(st.length-1))+'%';fill.style.height=f;dot.style.top=f;
if(clk)clk.textContent=i<0?'0 s':secs[i];}
place();addEventListener('resize',place);
if(matchMedia('(prefers-reduced-motion:reduce)').matches){show(st.length-1);return;}
function next(){i=i>=st.length-1?-1:i+1;show(i);setTimeout(next,i===st.length-1?4200:i<0?700:2100);}
next();})();
"""


def _icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden=true>{ICONS[name]}</svg>'


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
    secs = max(0, round((quote.sent_at - quote.rfq.received_at).total_seconds()))
    o, r = quote.option, quote.rfq
    return {
        "company": names.who(ctx, r.customer).name,
        "cargo": f"{r.weight_kg} kg · {o.origin} → {o.destination}",
        "options": options,
        "carrier": o.carrier,
        "total": money(quote.total, quote.currency),
        "secs": secs,
        "booked": bool(shipment),
        "ref": (shipment.booking_reference or shipment.id) if shipment else "",
    }


def render(ctx: Context) -> str:
    ex = example(ctx)
    if ex:
        notes = (
            f"<b>{esc(ex['company'])}</b> asks for a price",
            esc(ex["cargo"]) + ", read by rules",
            f"<b>{ex['options'] or 'Every'}</b> options compared, best: {esc(ex['carrier'])}",
            f"<b>{esc(ex['total'])}</b> all-in, sent after {ex['secs']} s",
            (f"Booked · <b>{esc(ex['ref'])}</b>, tracking answered from the CRM" if ex["booked"]
             else "Booked by rate id when the customer says yes"),
        )  # fmt: skip
        t = ex["secs"]
        clock = ["0 s", f"{max(1, t // 5)} s", f"{max(2, t * 3 // 5)} s", f"{t} s", "after the yes"]
        caption = (
            f"<span>A real request from <b>{esc(ex['company'])}</b>, as it ran through PIXEL</span>"
            "<span>Time since the mail arrived: <b class=clock>0 s</b></span>"
        )
    else:
        notes = ("A customer mails a request", "Weight, lane, pickup read", "Every price source compared",
                 "One all-in price sent", "Booked and tracked")  # fmt: skip
        clock = ["", "", "", "", ""]
        caption = "<span>Every request takes this path, day and night</span>"
    stations = (
        ("mail", "Request in"),
        ("read", "Reads"),
        ("price", "Prices"),
        ("send", "Replies"),
        ("book", "Books & tracks"),
    )
    cells = "".join(
        f"<div class=st5><div class=ic>{_icon(icon)}</div><h3>{esc(title)}</h3><div class=ex>{note}</div></div>"
        for (icon, title), note in zip(stations, notes, strict=True)
    )
    secs = "[" + ",".join(f'"{esc(c)}"' for c in clock) + "]"
    return (
        CSS
        + f"<div class=pipe id=pipe data-secs='{secs}'><div class=stations>"
        + f"<div class=rail><div class=fill></div><div class=dot></div></div>{cells}</div>"
        + f"<div class=cap>{caption}</div></div>"
    )
