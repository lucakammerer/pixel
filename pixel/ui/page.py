"""HTML for the sessions UI, the CRM and the dashboard: server-rendered, one stylesheet, a few lines of JS, no build."""

import json
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote as urlquote

from pixel.contracts import Event

CSS = """
:root{--bg:#faf9f7;--surface:#fff;--sunk:#f5f2ef;--ink:#14161c;--ink-2:#4a4540;--muted:#8a827b;--line:#ebe6e1;
--line-2:#d9d2cb;--accent:#d95f3b;--accent-2:#e8795a;--navy:#2b2622;--ok:#17804f;--warn:#b25b00;--bad:#c2331d;--human:#7a45d6;
--hover:#f8f5f2;--tint:#fbe6de;color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 Jost,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.top{height:60px;display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:28px;
padding:0 12px 0 24px;background:#fff;border:1px solid var(--line);border-radius:999px;position:sticky;top:14px;
z-index:10;max-width:1240px;margin:14px auto 0}
/* the wordmark from the pitch: an orange square and PIXEL in Satoshi Black */
.brand{display:flex;align-items:center;gap:10px;justify-self:start;color:var(--ink);font:900 20px/1 Satoshi,Jost,system-ui,
sans-serif;letter-spacing:-.01em;user-select:none}
.brand:hover{text-decoration:none}
.brand i{width:12px;height:12px;background:#DE5531;display:block;flex:none}
.top nav{display:grid;grid-template-columns:repeat(3,148px);gap:6px}
.top nav a{display:flex;align-items:center;justify-content:center;height:38px;padding:0 16px;border-radius:999px;color:var(--ink);
font-size:13px;font-weight:500;text-transform:uppercase;letter-spacing:.04em;transition:background .2s,color .2s}
.top nav a:hover{background:var(--hover);text-decoration:none}
.top nav a.on{background:var(--tint);color:var(--accent)}
.live{justify-self:end;display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}
.live i{width:7px;height:7px;border-radius:50%;background:var(--line-2);position:relative}
.live.on i{background:var(--ok)}.live.on i::after{content:"";position:absolute;inset:0;border-radius:50%;
background:var(--ok);animation:pulse 2s ease-out infinite}
@keyframes pulse{from{transform:scale(1);opacity:.55}to{transform:scale(3);opacity:0}}
@media (prefers-reduced-motion:reduce){.live.on i::after{animation:none}}
main{max-width:1240px;margin:0 auto;padding:28px 24px 80px}
.head{display:flex;align-items:flex-end;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.head .grow{flex:1;min-width:0}
h1,h2,h3{text-wrap:balance}b,strong{font-weight:500}
h1{font-size:20px;font-weight:500;margin:0;letter-spacing:-.015em;line-height:1.2}
h2{font-size:16px;font-weight:500;letter-spacing:-.01em;margin:40px 0 12px}h2 .count{color:var(--muted);font-weight:400;margin-left:4px}
.sub{color:var(--muted);margin-top:2px}.crumb{font-size:13px;color:var(--muted);margin-bottom:6px}
.crumb a{color:var(--muted)}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.num{font-variant-numeric:tabular-nums}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
.panel:has(>table){overflow-x:auto}
.panel.pad{padding:16px 18px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;font-weight:500;color:var(--muted);padding:9px 14px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tr[data-href]{cursor:pointer}tr[data-href]:hover td{background:var(--hover)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td a.id{color:var(--ink);font-weight:500}
td .line2{color:var(--muted);font-size:12px;margin-top:1px}
td.empty{color:var(--muted);text-align:center;padding:36px 14px}
.lane{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:nowrap}
.st{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;color:var(--ink-2)}
.st::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--line-2);flex:none}
.st.ok::before{background:var(--ok)}.st.warn::before{background:var(--warn)}.st.bad::before{background:var(--bad)}
.st.human::before{background:var(--human)}
.st.ok,.st.warn{color:var(--ink-2)}.st.bad{color:var(--bad)}
.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:4px;background:var(--sunk);color:var(--ink-2);
line-height:19px;white-space:nowrap;vertical-align:1px}.tag.human{color:var(--human)}.tag.warn{color:var(--warn)}
.due{font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
.due.ok{color:var(--ok)}.due.warn{color:var(--warn)}.due.muted{color:var(--muted)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.toolbar{display:flex;align-items:center;gap:16px;margin-bottom:12px;flex-wrap:wrap}
.tabs{display:flex;gap:4px;flex-wrap:wrap}
.tabs a{padding:5px 10px;border-radius:6px;color:var(--ink-2);font-size:13px}
.tabs a:hover{background:var(--sunk);text-decoration:none}.tabs a.on{background:var(--sunk);color:var(--ink);font-weight:500}
.tabs a span{color:var(--muted);margin-left:5px;font-variant-numeric:tabular-nums}
.tabrow{display:flex;align-items:flex-end;gap:16px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
.tabrow .search{margin-bottom:8px}.tabs.big{gap:22px}
.tabs.big a{padding:0 0 10px;border-radius:0;border-bottom:2px solid transparent;margin-bottom:-1px;font-size:14px}
.tabs.big a:hover{background:none;color:var(--ink)}.tabs.big a.on{background:none;border-color:var(--ink)}
input,textarea,button,select{font:inherit;color:inherit}
input[type=search],input[type=text],textarea{width:100%;padding:7px 10px;border:1px solid var(--line-2);border-radius:6px;
background:var(--surface)}
input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
.search{margin-left:auto;width:320px;max-width:100%}
textarea{min-height:200px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;line-height:1.5;
resize:vertical}
.btn{display:inline-flex;align-items:center;height:32px;padding:0 14px;border-radius:6px;border:1px solid var(--line-2);
background:var(--surface);color:var(--ink);cursor:pointer;font-weight:500;white-space:nowrap;font-size:13px}
.btn:hover{background:var(--hover)}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{opacity:.88}
form.inline{display:inline;margin:0}
.notice{display:flex;align-items:center;gap:14px;padding:11px 14px;border-radius:8px;margin-bottom:16px;
border:1px solid var(--line-2);background:var(--surface)}
.notice .grow{flex:1}.notice.bad{border-color:var(--bad);color:var(--bad)}
.notice.human{border-color:var(--human)}.notice.human b{color:var(--human)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:var(--surface);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
.stats>div{padding:14px 18px;border-right:1px solid var(--line);margin-right:-1px}
.stats .k{font-size:12px;color:var(--muted)}.stats .v{font-size:20px;font-weight:500;font-variant-numeric:tabular-nums;
letter-spacing:-.015em;margin-top:2px}.stats .note{font-size:12px;color:var(--muted)}
.split{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px;align-items:start}
.split>aside{position:sticky;top:76px}
.split>div>h2:first-child{margin-top:0;line-height:24px}.split:has(>div>h2:first-child)>aside{margin-top:36px}
dl{display:grid;grid-template-columns:84px 1fr;gap:8px 12px;margin:0}dt{color:var(--muted)}dd{margin:0;min-width:0;
overflow-wrap:anywhere}
.side h3{font-size:12px;font-weight:500;color:var(--muted);margin:0 0 10px}
.side .panel+.panel{margin-top:12px}
.quote{display:flex;gap:24px;align-items:flex-start;padding:18px}
.quote .total{font-size:28px;font-weight:500;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.15}
.quote .grow{flex:1}
.items{border-top:1px solid var(--line)}.items td{padding:8px 18px}.items tr:last-child td{font-weight:500}
tr.chosen td{background:var(--sunk)}
.ev{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line)}
.ev:last-child{border-bottom:0}.ev .t{color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px;padding-top:1px}
.ev .what{font-weight:500}.ev .what.ok{color:var(--ok)}.ev .what.bad{color:var(--bad)}.ev .what.human{color:var(--human)}
.ev .what.mail{color:var(--ink)}.ev .d{color:var(--ink-2)}.ev .kind{color:var(--muted);font-size:12px;padding-top:2px}
.ev.new{animation:flash 2s}@keyframes flash{from{background:var(--sunk)}to{background:transparent}}
.ev details summary{cursor:pointer;list-style:none}.ev details summary::-webkit-details-marker{display:none}
.ev details summary:hover .what{text-decoration:underline}
.evgroup{border-bottom:1px solid var(--line)}.evgroup:last-child{border-bottom:0}
.evgroup>summary{cursor:pointer;list-style:none;border-bottom:0}.evgroup>summary::-webkit-details-marker{display:none}
.evgroup>summary .what{font-weight:400;color:var(--muted)}.evgroup>summary .d{color:var(--muted)}
.evgroup>summary:hover .what{text-decoration:underline}.evgroup[open]>summary .kind{visibility:hidden}
.evgroup>.ev{background:var(--sunk);border-bottom:1px solid var(--line)}.evgroup>.ev:last-child{border-bottom:0}
.evearlier>.ev,.evearlier>.evgroup{background:none;border-bottom:1px solid var(--line)}
.evearlier[open]>summary{border-bottom:1px solid var(--line)}
.ev pre{white-space:pre-wrap;margin:8px 0 2px;padding:12px 14px;background:var(--sunk);border-radius:6px;font-size:13px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}
.draft{padding:16px 18px}.draft+.draft{border-top:1px solid var(--line)}
.field{display:grid;grid-template-columns:64px 1fr;align-items:center;gap:10px;margin-bottom:8px}
.field label{color:var(--muted)}
.actions{display:flex;align-items:center;gap:12px;margin-top:10px}.actions .grow{flex:1}
.handover,.actions{flex-wrap:wrap}
.handover .grow{flex-basis:26rem}
.handover p,.sourcing p{margin:6px 0 0;max-width:68ch}
.handover-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.handover-actions a{margin-right:8px;text-decoration:underline;text-underline-offset:3px}
.sourcing{margin-bottom:16px}.sourcing-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.sourcing-head h2{margin:0}
.steps{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:2px}
.step{display:grid;grid-template-columns:28px minmax(0,1fr);gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.step-n{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:500;
border:1px solid var(--line-2);color:var(--muted)}
.step.done .step-n{background:var(--ok);border-color:var(--ok);color:#fff}
.step.next .step-n{background:var(--accent);border-color:var(--accent);color:#fff}
.step.todo{color:var(--muted)}.step.todo b{font-weight:500}.step .handover-actions{margin-top:8px}
.step p{margin:2px 0 0}
.shipment-form,.followup{margin-top:12px}
.shipment-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:10px 14px}
.shipment-fields label,.mail-text{display:grid;gap:4px}
.shipment-fields select{width:100%;min-height:32px;background:var(--panel);border:1px solid var(--line-2);border-radius:6px;padding:5px 8px}
.shipment-form>.btn{margin-top:12px}
.followup>summary{cursor:pointer;padding:12px 18px}
.option-table{overflow-x:auto}.option-table td:first-child{overflow-wrap:anywhere}
.notice[hidden]{display:none}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.more{display:flex;justify-content:center;margin-top:16px}
.co{display:flex;align-items:center;gap:10px;min-width:0}.co>div{min-width:0}
.co .nm{color:var(--ink);font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.co .ml{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (max-width:900px){.split{grid-template-columns:minmax(0,1fr)}.split>aside{position:static}.search{width:100%;margin:0}
.top{gap:18px;padding:0 16px}.top nav{grid-template-columns:repeat(3,auto)}main{padding:20px 16px 60px}.hide-sm{display:none}
.ev{grid-template-columns:56px minmax(0,1fr)}.ev .kind{display:none}}
@media (max-width:420px){.top{height:auto;min-height:56px;flex-wrap:wrap;gap:0 10px;padding:0 10px}
.top{display:flex}.top .brand,.top .live{min-height:38px}.top .live{margin-left:auto}.top nav{order:1;width:100%;gap:10px;grid-template-columns:repeat(3,minmax(0,1fr))}
.top nav a{min-height:34px}.split .ev,.split dd{overflow-wrap:anywhere}}
"""

LIVE_JS = """
const dot=document.getElementById('live');
function live(url,onEvent){const es=new EventSource(url);
es.onopen=()=>{dot.classList.add('on');dot.lastChild.textContent='Live';};
es.onerror=()=>{dot.classList.remove('on');dot.lastChild.textContent='Reconnecting';};
es.onmessage=m=>onEvent(JSON.parse(m.data));}
async function reload(){const r=await fetch(location.href);
const doc=new DOMParser().parseFromString(await r.text(),'text/html');
for(const b of doc.querySelectorAll('[data-live]')){const a=document.getElementById(b.id);if(a)a.replaceWith(b);}
tick();}
function refresh(){let t;return()=>{clearTimeout(t);t=setTimeout(reload,400);};}
function tick(){for(const el of document.querySelectorAll('[data-deadline]')){
const s=Math.round((Date.parse(el.dataset.deadline)-Date.now())/1000);
el.textContent=s>0?Math.floor(s/60)+':'+String(s%60).padStart(2,'0')+' left':'expired';
el.className='due '+(s>120?'ok':s>0?'warn':'muted');}}
tick();setInterval(tick,1000);
document.addEventListener('click',e=>{const tr=e.target.closest('tr[data-href]');
if(!tr||e.target.closest('a,button,input,summary,form'))return;
if(e.metaKey||e.ctrlKey)window.open(tr.dataset.href);else location.href=tr.dataset.href;});
const box=document.querySelector('input.search');
if(box){let t;box.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(()=>{const u=new URL(location.href);
box.value?u.searchParams.set('q',box.value):u.searchParams.delete('q');history.replaceState(null,'',u);reload();},250);});}
"""

NAV = (("/requests", "Requests", "sessions"), ("/crm", "CRM", "crm"), ("/dashboard", "Performance", "dashboard"))


def esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def layout(title: str, body: str, *, nav: str = "", script: str = "") -> str:
    links = "".join(f'<a href="{href}" class="{"on" if nav == key else ""}">{label}</a>' for href, label, key in NAV)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · PIXEL</title>'
        f'<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600&display=swap" rel="stylesheet">'
        f'<link href="https://api.fontshare.com/v2/css?f[]=satoshi@900&display=swap" rel="stylesheet">'
        f'<style>{CSS}</style></head><body><header class=top><a class=brand href="/">'
        f"<i aria-hidden=true></i>PIXEL</a><nav>{links}</nav>"
        f"<span class=live id=live><i></i><span>Connecting</span></span></header>"
        f"<main>{body}</main><script>{LIVE_JS}{script}</script></body></html>"
    )


def page_head(title: str, sub: str = "", *, crumb: str = "", right: str = "") -> str:
    return (
        f"<div class=head><div class=grow>{f'<div class=crumb>{crumb}</div>' if crumb else ''}<h1>{title}</h1>"
        f"{f'<div class=sub>{sub}</div>' if sub else ''}</div>{right}</div>"
    )


def tabs(items: list[tuple[str, str, bool, int | None]], *, big: bool = False, live_id: str = "") -> str:
    """(href, label, active, count) links; `live_id` makes the bar refresh with the live stream."""
    attrs = f' id="{live_id}" data-live' if live_id else ""
    links = "".join(
        f'<a href="{href}" class="{"on" if on else ""}">{esc(label)}{f"<span>{n}</span>" if n is not None else ""}</a>'
        for href, label, on, n in items
    )
    return f'<div class="tabs{" big" if big else ""}"{attrs}>{links}</div>'


def search_box(q: str, placeholder: str) -> str:
    return (
        f'<form role=search class=search><input class=search type=search name=q value="{esc(q)}" '
        f'placeholder="{esc(placeholder)}" autocomplete=off></form>'
    )


def stats(cells: list[tuple[str, Any, str]]) -> str:
    return (
        "<div class=stats>"
        + "".join(
            f"<div><div class=k>{esc(k)}</div><div class=v>{esc('—' if v is None else v)}</div>"
            f"{f'<div class=note>{note}</div>' if note else ''}</div>"
            for k, v, note in cells
        )
        + "</div>"
    )


def status(label: str, css: str) -> str:
    return f'<span class="st {css}">{esc(label)}</span>'


def lane(origin: Any, destination: Any) -> str:
    return f"<span class=lane>{esc(origin)} → {esc(destination)}</span>" if origin else ""


def customer_href(email: str) -> str:
    return f"/crm/customers/{urlquote(email.lower(), safe='@')}"


def company(who: Any, *, href: str | None = None, line2: str | None = None) -> str:
    """Company name (linked) and a muted second line: the person who signs, else the address."""
    name = f'<a class=nm href="{href}">{esc(who.name)}</a>' if href else f"<div class=nm>{esc(who.name)}</div>"
    sub = line2 if line2 is not None else esc(" · ".join(x for x in (who.contact, who.city) if x) or who.email)
    return f"<div class=co><div>{name}<div class=ml>{sub}</div></div></div>"


def customer_link(email: str | None, who: Any = None) -> str:
    if not email:
        return ""
    label = who.name if who else email
    title = f' title="{esc(email)}"' if who else ""
    return f'<a href="{customer_href(email)}"{title}>{esc(label)}</a>'


def clock(ts: datetime | str | None) -> str:
    if not ts:
        return ""
    when = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone().strftime("%H:%M:%S")


def when(ts: datetime | str | None) -> str:
    """Time of day when it was today, else day and time."""
    if not ts:
        return ""
    at = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    at = at.astimezone()
    return at.strftime("%H:%M") if at.date() == datetime.now().astimezone().date() else at.strftime("%d %b, %H:%M")


def days(n: Any) -> str:
    return f"{esc(n)} day" if str(n) == "1" else f"{esc(n)} days"


def money(amount: Any, currency: str | None = "") -> str:
    if amount is None or amount == "":
        return "—"
    return f"{esc(currency or '')} {float(amount):,.2f}".strip()


LABELS = {
    "mail.in": "Mail received", "mail.out": "Mail sent", "mail.held": "Mail held", "route": "Sorted",
    "rfq.extracted": "Request read", "option.found": "Option priced", "option.failed": "Option failed",
    "bid.requested": "Bid requested", "bid.received": "Bid received", "quote.drafted": "Quote drafted",
    "quote.sent": "Quote sent", "quote.fallback": "Fallback quote sent", "quote.missed": "Deadline missed",
    "customer.accepted": "Customer accepted", "customer.declined": "Customer declined",
    "booking.requested": "Booking requested", "booking.booked": "Booked", "booking.confirmed": "Airline confirmed",
    "booking.failed": "Booking failed", "tracking.answered": "Tracking answered", "tariff.ingested": "Tariff loaded",
    "tariff.failed": "Tariff failed", "followup.read": "Reply read", "llm.call": "Model call", "score": "Scored", "human.note": "Sent by a person",
    "human.takeover": "Taken over", "human.release": "Back on autopilot", "error": "Error",
    "rfq.dangerous_goods": "Dangerous goods", "quote.prose": "Quote text written", "prose.raw": "Model text",
    "feedback": "Scorer feedback", "bid.skipped": "Bid skipped", "carrier.lanes": "Lanes learned",
    "call.ringing": "Phone ringing", "call.started": "Voice call started", "call.connected": "Voice call connected",
    "call.turn": "Call transcript", "call.answer": "Voice answer saved", "call.ended": "Voice call ended",
    "call.transcript": "Call summary", "shipment.updated": "Order status updated",
}  # fmt: skip


# What a person follows on a session's timeline: the request as read, the quote, the customer's answer, booking,
# people and failures (`ui.routes.background` adds which mail). Every other kind is a background step, and the timeline folds each run of them into
# one line, so a new kind folds by default.
KEY = frozenset({
    "mail.in", "mail.out", "mail.held", "rfq.extracted", "rfq.dangerous_goods", "quote.sent",
    "quote.fallback", "quote.missed", "quote.stuck", "tracking.answered", "followup.read", "error", "call.answer",
    "call.transcript", "shipment.updated", "feedback", "score", "attachment.unreadable",
})  # fmt: skip
KEY_PREFIXES = ("customer.", "booking.", "human.", "tariff.")


def is_minor(kind: str, payload: dict | None = None) -> bool:
    """A background step; a check that blocked the quote or unmet asks that raised an alert stay visible."""
    p = payload or {}
    if (kind == "quote.checked" and p.get("blocking")) or (kind == "quote.unmet" and p.get("alert")):
        return False
    return kind not in KEY and not kind.startswith(KEY_PREFIXES)


def minor_summary(kinds: list[str]) -> str:
    """The folded line's text: how many steps, then each kind with its count, most frequent first."""
    counts: dict[str, int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    parts = ", ".join(
        LABELS.get(k, esc(k)).lower() + (f" ×{n}" if n > 1 else "")
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    noun = "step" if len(kinds) == 1 else "steps"
    return f"<span class=what>{len(kinds)} automatic {noun}</span> <span class=d>· {parts}</span>"


LATEST = 6  # entries the timeline shows; earlier ones fold into one line at its top


def group_events(evs: list[Event], rows: list[str], minor: list[bool]) -> str:
    """The timeline: every row of `evs` in order, with each run of background rows (`minor`) folded into one
    <details>, and every entry before the LATEST folded into one more at the top."""
    out: list[str] = []
    run: list[int] = []

    def flush() -> None:
        if len(run) == 1:
            out.append(rows[run[0]])
        elif run:
            out.append(
                f"<details class=evgroup><summary class=ev><span class=t>{clock(evs[run[0]].ts)}</span>"
                f"<div>{minor_summary([evs[i].kind for i in run])}</div><span class='kind mono'>show all</span></summary>"
                + "".join(rows[i] for i in run) + "</details>"
            )  # fmt: skip
        run.clear()

    for i, ev in enumerate(evs):
        if minor[i]:
            run.append(i)
        else:
            flush()
            out.append(rows[i])
    flush()
    if len(out) <= LATEST:
        return "".join(out)
    earlier = out[:-LATEST]
    return (
        f"<details class='evgroup evearlier'><summary class=ev><span class=t></span><div><span class=what>"
        f"Show {len(earlier)} earlier entries</span></div><span class='kind mono'>show all</span></summary>"
        + "".join(earlier) + "</details>" + "".join(out[-LATEST:])
    )  # fmt: skip


def kind_class(kind: str) -> str:
    if kind.startswith("mail."):
        return "mail"
    if kind.startswith(("quote.sent", "booking.booked", "booking.confirmed", "customer.accepted", "shipment.updated", "call.answer")):
        return "ok"
    if kind.startswith(("human.", "call.")):
        return "human"
    if kind in ("error", "quote.missed", "booking.failed", "option.failed", "tariff.failed", "quote.fallback"):
        return "bad"
    return ""


def _plain(value: Any) -> str:
    text = value if not isinstance(value, (dict, list)) else json.dumps(value)
    return esc(text)[:300]


def event_row(ev: Event, mail: dict[str, Any] | None = None, *, new: bool = False, minor: bool | None = None) -> str:
    p = ev.payload or {}
    what = f'<span class="what {kind_class(ev.kind)}">{LABELS.get(ev.kind, esc(ev.kind))}</span>'
    if mail:
        who = f"{esc(mail['sender'])} → {esc(mail['recipient'])}"
        main = (
            f"<details><summary>{what} <span class=d>{esc(mail['subject'])}</span>"
            f"<div class=muted>{who}</div></summary><pre>{esc(mail['body'])}</pre></details>"
        )
    else:
        if ev.kind == "mail.held":
            detail = f"{esc(p.get('subject'))} <span class=muted>to {esc(p.get('to'))} · {esc(p.get('reason'))}</span>"
        elif ev.kind == "llm.call":
            detail = (
                f"{esc(p.get('purpose', ''))} <span class=muted>· {esc(p.get('model', ''))} · "
                f"{ev.tokens_in}+{ev.tokens_out} tokens · {ev.duration_ms} ms ·</span> ${float(ev.cost_usd):.5f}"
            )
        elif ev.kind == "option.found":
            o = p.get("option", p)
            detail = (
                f"{esc(o.get('carrier'))} <span class=muted>via {esc(o.get('source'))}</span> · "
                f"<span class=num>{money(o.get('total'), o.get('currency'))}</span>"
                f" <span class=muted>· {days(o.get('transit_days'))} · billed {esc(o.get('billed_kg'))} kg</span>"
            )
        elif ev.kind == "rfq.extracted" and p.get("origin") and p.get("destination"):
            units = (("weight_kg", " kg"), ("volume_cbm", " cbm"), ("pieces", " pcs"), ("preference", ""))
            bits = [f"<span class=mono>{esc(p['origin'])} → {esc(p['destination'])}</span>"]
            bits += [f"{esc(p[k])}{unit}" for k, unit in units if p.get(k)]
            bits += ["door pickup"] if p.get("door_pickup") else []
            bits += [f"<span class=muted>read by</span> {esc(p['extracted_by'])}"] if p.get("extracted_by") else []
            missing = ", ".join(map(str, p.get("missing") or []))
            bits += [f"<span class=bad>missing {esc(missing)}</span>"] if missing else []
            detail = " · ".join(bits)
        elif ev.kind == "route" and "kind" in p:
            detail = f"<span class=muted>as</span> {esc(str(p['kind']).replace('_', ' '))}"
        elif ev.kind == "shipment.updated":
            bits = [
                f"<span class=mono>{esc(p.get('shipment_id', ''))}</span>",
                f"<b>{esc(p.get('status_label') or p.get('status', ''))}</b>",
                *(esc(x) for x in (p.get("location"), p.get("eta"), p.get("flight_number")) if x),
            ]
            detail = " · ".join(bits)
        elif ev.kind == "call.turn":
            who = "Agent" if p.get("role") == "agent" else "Caller"
            detail = f"<span class=muted>{who}:</span> {esc(p.get('message', ''))}"
        elif ev.kind in ("call.ringing", "call.started"):
            detail = f"{esc(p.get('name', ''))} <span class=muted>· {esc(p.get('shipment_id') or p.get('party', ''))}</span>"
        elif ev.kind == "call.transcript":
            detail = esc(p.get("summary") or f"{len(p.get('transcript') or [])} turns")
        else:
            detail = " · ".join(
                f"<span class=muted>{esc(k).replace('_', ' ')}</span> {_plain(v)}" for k, v in p.items()
            )
        main = f"{what} <span class=d>{detail}</span>" if detail else what
    actor = f"{esc(ev.kind)} · {esc(ev.actor)}"
    minor = is_minor(ev.kind, ev.payload) if minor is None else minor
    cls = "ev" + (" minor" if minor else "") + (" new" if new else "")
    return (
        f'<div class="{cls}" id="ev{ev.id}"><span class=t>{clock(ev.ts)}</span>'
        f"<div>{main}</div><span class='kind mono'>{actor}</span></div>"
    )
