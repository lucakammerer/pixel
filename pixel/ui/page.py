"""HTML for the sessions UI and the dashboard: server-rendered, one stylesheet, a few lines of JS, no build."""

import json
from datetime import UTC, datetime
from html import escape
from typing import Any

from pixel.contracts import Event

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#2f5bd3;--ok:#1f7a4d;
--warn:#a86200;--bad:#b3261e;--soft:#eef2fd;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1d1d1d;--ink:#ececea;--muted:#9a9a94;--line:#2e2e2c;
--accent:#7c9cff;--ok:#5fc48f;--warn:#e0a344;--bad:#ff7b72;--soft:#1f2638}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;gap:20px;align-items:center;padding:12px 24px;border-bottom:1px solid var(--line);
background:var(--card);position:sticky;top:0;z-index:1}
header b{letter-spacing:.08em}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--ink);font-weight:600}
header .live{margin-left:auto;font-size:13px;color:var(--muted)}
main{max-width:1180px;margin:0 auto;padding:20px 24px 60px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
margin:28px 0 8px}a{color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--soft)}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:13px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line);
white-space:nowrap}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .k{font-size:12px;color:var(--muted)}.card .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
form.search{display:flex;gap:8px;margin:0 0 14px}input,textarea,button{font:inherit;color:inherit}
input[type=search],input[type=text],textarea{flex:1;width:100%;padding:8px 10px;border:1px solid var(--line);
border-radius:6px;background:var(--card)}textarea{min-height:180px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
button{padding:7px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--accent)}
.ev{display:grid;grid-template-columns:70px 150px 1fr;gap:10px;padding:7px 10px;border-bottom:1px solid var(--line)}
.ev:last-child{border-bottom:0}.ev .t{color:var(--muted);font-variant-numeric:tabular-nums;font-size:13px}
.ev .kind{font-family:ui-monospace,Menlo,monospace;font-size:12px}.ev.new{animation:flash 2s}
.ev details summary{cursor:pointer}.ev pre{white-space:pre-wrap;margin:6px 0 0;font-size:13px}
@keyframes flash{from{background:var(--soft)}to{background:transparent}}
.k-mail{color:var(--accent)}.k-quote{color:var(--ok)}.k-llm{color:var(--warn)}.k-bad{color:var(--bad)}.k-human{color:#8a3ffc}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.spacer{flex:1}
.banner{padding:10px 14px;border-radius:8px;border:1px solid var(--bad);color:var(--bad);margin-bottom:14px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:800px){.cols{grid-template-columns:1fr}
.ev{grid-template-columns:60px 1fr}.ev .kind{grid-column:2}}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0}dt{color:var(--muted)}dd{margin:0}
"""

LIVE_JS = """
const dot=document.getElementById('live');
function live(url,onEvent){const es=new EventSource(url);
es.onopen=()=>dot.textContent='● live';es.onerror=()=>dot.textContent='○ reconnecting';
es.onmessage=m=>onEvent(JSON.parse(m.data));}
function refresh(){let t;return()=>{clearTimeout(t);t=setTimeout(async()=>{const r=await fetch(location.href);
const doc=new DOMParser().parseFromString(await r.text(),'text/html');
for(const id of ['refresh']){const a=document.getElementById(id),b=doc.getElementById(id);if(a&&b)a.replaceWith(b);}
tick();},400);};}
function tick(){for(const el of document.querySelectorAll('[data-deadline]')){
const s=Math.round((Date.parse(el.dataset.deadline)-Date.now())/1000);
el.textContent=s>0?Math.floor(s/60)+':'+String(s%60).padStart(2,'0')+' left':'expired';
el.className='pill '+(s>120?'ok':s>0?'warn':'muted');}}
tick();setInterval(tick,1000);
"""


def esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def layout(title: str, body: str, *, nav: str = "", script: str = "") -> str:
    def link(href: str, label: str, key: str) -> str:
        return f'<a href="{href}" class="{"on" if nav == key else ""}">{label}</a>'

    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · PIXEL</title>'
        f"<style>{CSS}</style></head><body><header><b>PIXEL</b>{link('/', 'Sessions', 'sessions')}"
        f'{link("/dashboard", "Dashboard", "dashboard")}<span class="live" id="live">○ connecting</span></header>'
        f"<main>{body}</main><script>{LIVE_JS}{script}</script></body></html>"
    )


def clock(ts: datetime | str | None) -> str:
    if not ts:
        return ""
    when = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone().strftime("%H:%M:%S")


def money(amount: Any, currency: str | None = "") -> str:
    if amount is None or amount == "":
        return "—"
    return f"{esc(currency or '')} {float(amount):,.2f}".strip()


def kind_class(kind: str) -> str:
    if kind.startswith("mail."):
        return "k-mail"
    if kind.startswith(("quote.sent", "booking.booked", "booking.confirmed", "customer.accepted")):
        return "k-quote"
    if kind == "llm.call":
        return "k-llm"
    if kind.startswith("human."):
        return "k-human"
    if kind in ("error", "quote.missed", "booking.failed", "option.failed", "tariff.failed", "quote.fallback"):
        return "k-bad"
    return ""


def event_row(ev: Event, mail: dict[str, Any] | None = None, *, new: bool = False) -> str:
    p = ev.payload or {}
    if mail:
        who = f"{esc(mail['sender'])} → {esc(mail['recipient'])}"
        summary = f"<b>{esc(mail['subject'])}</b> <span class=muted>{who}</span>"
        detail = f"<details><summary>{summary}</summary><pre>{esc(mail['body'])}</pre></details>"
    elif ev.kind == "mail.held":
        detail = f"<b>Held:</b> {esc(p.get('subject'))} <span class=muted>to {esc(p.get('to'))} · {esc(p.get('reason'))}</span>"
    elif ev.kind == "llm.call":
        detail = (
            f"{esc(p.get('purpose', ''))} <span class=muted>{esc(p.get('model', ''))} · {ev.tokens_in}+{ev.tokens_out} "
            f"tokens · {ev.duration_ms} ms</span> <b>${float(ev.cost_usd):.5f}</b>"
        )
    elif ev.kind == "option.found":
        o = p.get("option", p)
        detail = (
            f"{esc(o.get('source'))} · {esc(o.get('carrier'))} · <b>{money(o.get('total'), o.get('currency'))}</b>"
            f" <span class=muted>{esc(o.get('transit_days'))} d · billed {esc(o.get('billed_kg'))} kg</span>"
        )
    else:
        detail = " · ".join(
            f"<span class=muted>{esc(k)}</span> {esc(v if not isinstance(v, (dict, list)) else json.dumps(v))[:300]}"
            for k, v in p.items()
        )
    return (
        f'<div class="ev{" new" if new else ""}" id="ev{ev.id}"><span class=t>{clock(ev.ts)}</span>'
        f'<span class="kind {kind_class(ev.kind)}">{esc(ev.kind)}<br><span class=muted>{esc(ev.actor)}</span></span>'
        f"<div>{detail}</div></div>"
    )
