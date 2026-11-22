"""Sessions UI: find a session, watch it live, step in. Reads the event log only."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from urllib.parse import quote as urlquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse

from pixel.agents.rfq import compose
from pixel.api.client import ApiError
from pixel.api.mailbox import RATEDESK
from pixel.contracts import Event, MailKind, QuoteStatus, Rfq
from pixel.core import events, outbox, timing
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.pricing import engine
from pixel.pricing import session as pricing_session
from pixel.ui import names, operator
from pixel.ui.page import (
    clock,
    company,
    customer_link,
    days,
    esc,
    event_row,
    group_events,
    is_minor,
    lane,
    layout,
    money,
    page_head,
    search_box,
    status,
    tabs,
    when,
)  # fmt: skip
from pixel.voice import calls as voice

router = APIRouter()


@router.get("/assets/pixel-logo.png", include_in_schema=False)
def pixel_logo() -> FileResponse:
    return FileResponse(Path(__file__).with_name("logo.png"), media_type="image/png")


STATUS = (  # first match wins: (event kind, label, css class)
    ("booking.confirmed", "confirmed", "ok"), ("booking.booked", "booked", "ok"),
    ("booking.failed", "booking failed", "bad"), ("customer.declined", "declined", ""),
    ("customer.accepted", "accepted", "ok"), ("quote.missed", "missed", "bad"),
    ("quote.fallback", "quoted · fallback", "warn"), ("quote.sent", "quoted", "ok"),
    ("tracking.answered", "tracking", ""), ("tariff.ingested", "tariff", ""),
    ("tariff.failed", "tariff failed", "bad"),
)  # fmt: skip
FIRST = 8  # requests shown before "See more"
FILTERS = ("open", "quoted", "accepted", "booked", "confirmed", "booking", "declined", "missed", "tracking", "tariff")


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


def status_of(kinds: set[str]) -> tuple[str, str]:
    for kind, label, css in STATUS:
        if kind in kinds:
            return label, css
    return "open", "warn"


def list_sessions(ctx: Context, q: str = "", state: str = "", limit: int = 300, customer: str = "") -> list[dict]:
    rows = ctx.db.execute(
        "SELECT e.session_id AS id, MIN(e.ts) AS first_ts, MAX(e.ts) AS last_ts, GROUP_CONCAT(DISTINCT e.kind) AS kinds,"
        " SUM(CAST(e.cost_usd AS REAL)) AS cost, m.sender, m.subject, s.mode, s.deadline_at"
        " FROM events e LEFT JOIN messages m ON m.id = e.session_id LEFT JOIN sessions s ON s.id = e.session_id"
        " WHERE e.session_id IS NOT NULL GROUP BY e.session_id ORDER BY first_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    found = []
    needle = q.strip().lower()
    for row in rows:
        if customer and (row["sender"] or "").lower() != customer.lower():
            continue
        quote = quotes.for_session(ctx, row["id"])
        shipment = shipments.for_session(ctx, row["id"])
        label, css = status_of(set((row["kinds"] or "").split(",")))
        lane_text = f"{quote.option.origin} → {quote.option.destination}" if quote else ""
        email = (quote.rfq.customer if quote else row["sender"]) or ""
        who = names.who(ctx, email)
        item = {
            "who": who, "id": row["id"], "first_ts": row["first_ts"], "last_ts": row["last_ts"], "cost": row["cost"] or 0,
            "customer": (quote.rfq.customer if quote else row["sender"]) or "", "subject": row["subject"] or "",
            "lane": lane_text, "quote": quote, "shipment": shipment, "status": label, "css": css,
            "mode": row["mode"] or "auto", "deadline": row["deadline_at"] if label == "open" else None,
        }  # fmt: skip
        haystack = " ".join(
            str(x) for x in (
                item["id"], item["customer"], who.name, who.contact, item["subject"], lane_text, lane_text.replace(" → ", "-"), label,
                quote and quote.id, quote and quote.option.carrier, shipment and shipment.id,
                shipment and shipment.booking_reference,
            ) if x
        ).lower()  # fmt: skip
        if needle and not all(word in haystack for word in needle.split()):
            continue
        if state and state != label.split(" ")[0]:
            continue
        found.append(item)
    return found


def sessions_table(items: list[dict], *, show_customer: bool = True) -> str:
    """The table of requests; its body has id `refresh` so the live stream can swap it."""
    head = (
        "<thead><tr><th>Received</th>" + ("<th>Customer</th>" if show_customer else "<th>Subject</th>")
        + "<th>Lane</th><th class=n>Quote</th><th>Status</th><th class=hide-sm>References</th></tr></thead>"
    )  # fmt: skip
    if not items:
        body = "<tr><td colspan=6 class=empty>No request matches.</td></tr>"
    else:
        rows = []
        for s in items:
            quote, shipment = s["quote"], s["shipment"]
            href = f"/sessions/{s['id']}"
            extra = ""
            if s["mode"] == "human":
                extra += " <span class='tag human'>person</span>"
            if s["deadline"]:
                extra += f' <span data-deadline="{esc(s["deadline"])}" class=due></span>'
            refs = " ".join(esc(r) for r in (shipment and shipment.id, shipment and shipment.booking_reference) if r)
            who = (
                company(s["who"], href=href, line2=esc(s["subject"])[:90])
                if show_customer
                else f"<a class=id href='{href}'>{esc(s['subject'])[:90] or s['id']}</a>"
            )
            rows.append(
                f'<tr data-href="{href}"><td class=num>{when(s["first_ts"])}<div class=line2>#{s["id"]}</div></td>'
                f"<td>{who}</td><td>{lane(quote.option.origin, quote.option.destination) if quote else ''}</td>"
                f"<td class=n>{money(quote.total, quote.currency) if quote else '<span class=muted>—</span>'}</td>"
                f"<td>{status(s['status'], s['css'])}{extra}</td><td class='mono muted hide-sm'>{refs}</td></tr>"
            )
        body = "".join(rows)
    return f'<table>{head}<tbody id="refresh" data-live>{body}</tbody></table>'


@router.get("/requests", response_class=HTMLResponse)
async def sessions(request: Request, q: str = "", state: str = "", more: bool = False):
    """The list: newest first, searchable by customer, lane, reference; time left on open requests."""
    ctx = ctx_of(request)
    matching = list_sessions(ctx, q)
    counts: dict[str, int] = {}
    for s in matching:
        key = s["status"].split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
    items = [s for s in matching if not state or s["status"].split(" ")[0] == state]
    hidden = 0 if more else max(0, len(items) - FIRST)
    shown = items if more else items[:FIRST]

    def href(value: str) -> str:
        params = {k: v for k, v in (("q", q), ("state", value)) if v}
        return "/requests" + (f"?{urlencode(params)}" if params else "")

    bar = tabs(
        [(href(""), "All", not state, len(matching))]
        + [(href(f), f.capitalize(), state == f, counts.get(f, 0)) for f in FILTERS if counts.get(f) or state == f],
        live_id="tabs",
    )
    body = (
        page_head("Requests", "Every customer thread, newest first. Open requests show the time left.")
        + f"<div class=toolbar>{bar}{search_box(q, 'Search customer, lane, reference')}</div>"
        + f"<div class=panel>{sessions_table(shown)}</div>"
    )
    if hidden:
        params = {k: v for k, v in (("q", q), ("state", state)) if v} | {"more": "1"}
        body += (
            f"<div class=more><a class=btn href='/requests?{urlencode(params)}'>See more"
            f"<span class=muted>&nbsp;{hidden}</span></a></div>"
        )
    return layout("Requests", body, nav="sessions", script="live('/events',refresh());")


def _mails(ctx: Context, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = ctx.db.execute(f"SELECT id, sender, recipient, subject, body, kind FROM messages WHERE id IN ({marks})", ids)
    return {r["id"]: dict(r) for r in rows}


def background(ev: Event, mails: dict[int, dict]) -> bool:
    """A background step of the timeline: an internal kind, or mail with someone other than whoever opened the
    thread (a carrier's bid, the rate desk), which a person rarely needs to follow."""
    if ev.kind not in ("mail.in", "mail.out"):
        return is_minor(ev.kind)
    mail, first = mails.get(ev.message_id or 0), mails.get(ev.session_id or 0)
    if not mail or not first:
        return False
    other = mail["recipient"] if ev.kind == "mail.out" else mail["sender"]
    return other.lower() != first["sender"].lower()


def render_events(ctx: Context, evs: list[Event], *, new: bool = False) -> list[tuple[str, bool]]:
    """Each event's row and whether it is a background step."""
    mail_evs = [e for e in evs if e.message_id and e.kind in ("mail.in", "mail.out")]
    mails = _mails(ctx, list({i for e in mail_evs for i in (e.message_id, e.session_id) if i}))
    flags = [background(e, mails) for e in evs]
    return [
        (event_row(e, mails.get(e.message_id) if e in mail_evs else None, new=new, minor=minor), minor)
        for e, minor in zip(evs, flags, strict=True)
    ]


def held_drafts(evs: list[Event]) -> list[Event]:
    """Mail the outbox held because a person took over (not dry run), and nobody has sent since."""
    sent_after = max(
        (e.id or 0 for e in evs if e.kind == "human.note" and e.payload.get("action") == "sent"), default=0
    )
    return [e for e in evs if e.kind == "mail.held" and e.payload.get("reason") == "human" and (e.id or 0) > sent_after]


def _quote_panel(quote) -> str:
    if not quote:
        return '<div class="panel pad muted">No quote yet. Options appear below as carriers are priced.</div>'
    o = quote.option
    state = "draft, not sent" if quote.sent_at is None else str(quote.status)
    lines = [("Freight", o.freight), ("Fuel", o.fuel), ("Security", o.security)]
    if o.pickup:
        lines.append(("Pickup" + (f" · {o.trucker}" if o.trucker else ""), o.pickup))
    items = "".join(f"<tr><td>{esc(k)}</td><td class=n>{money(v, o.currency)}</td></tr>" for k, v in lines)
    items += f"<tr><td>All-in total</td><td class=n>{money(quote.total, quote.currency)}</td></tr>"
    sent = f" · sent {clock(quote.sent_at)}" if quote.sent_at else ""
    return (
        "<div class=panel><div class=quote><div class=grow><div class=muted>Quote "
        f"<span class=mono>{esc(quote.id)}</span>{sent}</div><div class=total>{money(quote.total, quote.currency)}</div>"
        f"<div>{esc(o.carrier)} <span class=muted>· via {esc(o.source)} · {days(o.transit_days)} · "
        f"billed {esc(o.billed_kg)} kg</span></div></div><div>{status(state, 'ok' if quote.sent_at else 'warn')}"
        + (" <span class='tag warn'>fallback</span>" if quote.fallback else "")
        + f"</div></div><table class=items>{items}</table></div>"
    )


def _details(ctx: Context, session_id: int, r: Rfq | None, first: dict, deadline, calls: int, cost) -> str:
    customer = r.customer if r else first.get("sender", "")
    facts = [("Customer", customer_link(customer, names.who(ctx, customer)))]
    if r:
        facts += [
            ("Lane", lane(r.origin, r.destination)),
            (
                "Cargo",
                f"{esc(r.weight_kg)} kg gross · {esc(r.volume_cbm)} cbm"
                + (f" · {r.pieces} pieces" if r.pieces else ""),
            ),
            ("Preference", esc(r.preference)),
            ("Pickup", f"door · {esc(r.pickup_postcode or '')}" if r.door_pickup else "airport to airport"),
            ("Reply language", esc(r.language)),
            ("Currency", esc(r.currency or "Any")),
            ("Received", clock(r.received_at)),
        ]
        if r.required_carriers:
            facts.append(("Required airlines", esc(", ".join(r.required_carriers))))
        if r.ready_on:
            facts.append(("Ready on", esc(r.ready_on)))
        if r.arrive_by:
            facts.append(("Arrive by", esc(r.arrive_by)))
        if r.max_transit_days is not None:
            facts.append(("Transit limit", days(r.max_transit_days)))
    if deadline:
        facts.append(("Deadline", f'<span data-deadline="{esc(deadline)}" class=due></span>'))
    facts.append(("Model", f"{calls} call{'s' if calls != 1 else ''} · ${float(cost):.5f}"))
    shipment, booking = shipments.for_session(ctx, session_id), bookings.for_session(ctx, session_id)
    side = '<div class="panel pad"><h3>Request</h3><dl>'
    side += "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl></div>"
    if shipment or booking:
        rows = []
        if shipment:
            latest = voice.latest_order_update(ctx, shipment.id)
            meta = " · ".join(x for x in (latest.get("location"), latest.get("eta"), latest.get("flight_number")) if x) if latest else ""
            rows += [
                ("Shipment", f"<span class=mono>{esc(shipment.id)}</span>"),
                ("Status", f"<span data-session-shipment-status='{esc(shipment.id)}'>{esc(voice.status_label(shipment.status))}</span>"),
                ("Live update", f"<span data-session-shipment-meta='{esc(shipment.id)}'>{esc(meta or '—')}</span>"),
            ]
        if booking:
            rows += [
                ("Airline ref", f"<span class=mono>{esc(booking.booking_reference or '—')}</span>"),
                ("Booked via", esc(booking.via)), ("Booking", esc(booking.status)),
            ]  # fmt: skip
        side += '<div class="panel pad"><h3>Booking</h3><dl>'
        side += "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl></div>"
    return f"<aside class=side>{side}</aside>"


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session(request: Request, session_id: int, error: str = "", result: str = ""):
    """One session: mails, decisions, options with their prices, model calls with cost, the quote."""
    return _session_page(ctx_of(request), session_id, error, result)


def _session_page(ctx: Context, session_id: int, error: str, result: str = "", entered: dict | None = None) -> str:
    evs = events.for_session(ctx, session_id)
    if not evs:
        raise HTTPException(404, "no such session")
    row = ctx.db.execute(
        "SELECT mode, deadline_at, quote_message_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    mode = row["mode"] if row else "auto"
    quote = quotes.for_session(ctx, session_id)
    first = _mails(ctx, [session_id]).get(session_id, {})
    label, css = status_of({e.kind for e in evs})
    cost = sum(e.cost_usd for e in evs)
    calls = sum(1 for e in evs if e.kind == "llm.call")
    deadline = row["deadline_at"] if row and label == "open" else None
    is_rfq = first.get("kind") == MailKind.CUSTOMER_RFQ
    rfq = operator.request_facts(ctx, session_id) or (quote.rfq if quote else None)
    sent = bool(quote and quote.sent_at or row and row["quote_message_id"])
    priced = bool(quote and quote.sent_at is None and quote.status == QuoteStatus.DRAFT and not sent)

    if mode == "human":
        right = status(label, css)
        notice = (
            "<div class='notice human handover'><div class=grow><b>You’re handling this request</b>"
            "<p>PIXEL sends nothing to the customer without you; carrier replies keep coming in. If no quote is out "
            "by the deadline, PIXEL still sends its best price, so the customer always gets an answer.</p></div>"
            f'<form class=inline method=post action="/sessions/{session_id}/release">'
            "<button class=btn>Release to autopilot</button></form></div>"
        )
    else:
        right = (
            f"{status(label, css)}<form class=inline method=post action='/sessions/{session_id}/takeover'>"
            "<button class=btn title='Hold automatic mail for this request so you can edit and send it yourself'>"
            "Take over</button></form>"
        )
        notice = ""
    title = esc(first.get("subject") or f"Session {session_id}")
    customer = quote.rfq.customer if quote else first.get("sender", "")
    head = page_head(
        title, f"#{session_id} · {customer_link(customer, names.who(ctx, customer))}",
        crumb="<a href='/requests'>Requests</a>",
        right=f"<div style='display:flex;gap:14px;align-items:center'>{right}</div>",
    )  # fmt: skip
    banner = f"<div class='notice bad' role='alert'>{esc(error)}</div>" if error else ""
    if result:
        banner += f"<div class='notice' role='status'>{esc(result)}</div>"

    options = [e.payload.get("option", e.payload) for e in evs if e.kind == "option.found"]
    chosen = (quote.option.carrier, str(quote.option.total)) if quote else None
    options_rows = "".join(
        f"<tr class='{'chosen' if chosen == (o.get('carrier'), str(o.get('total'))) else ''}'>"
        f"<td>{esc(o.get('carrier'))}"
        + (" <span class=tag>quoted</span>" if chosen == (o.get("carrier"), str(o.get("total"))) else "")
        + (
            f"<div class=muted>Rate ID {esc(o['rate_id'])} · valid until {clock(o.get('valid_until'))}</div>"
            if o.get("rate_id")
            else ""
        )
        + f"</td><td class=muted>{'pickup bid' if o.get('trucker') else esc(o.get('source'))}</td>"
        f"<td class=n>{esc(o.get('billed_kg'))}</td>"
        f"<td class=n>{esc(o.get('transit_days'))}</td><td class=n>{money(o.get('total'), o.get('currency'))}</td>"
        f"<td class='n hide-sm'>{money(o.get('total_eur'), 'EUR')}</td></tr>"
        for o in sorted(options, key=lambda o: float(o.get("total_eur") or o.get("total") or 0))
    )
    options_html = (
        f"<h2 id=options>Options<span class=count>{len(options)}</span></h2>"
        "<div class='panel option-table'><table><thead><tr><th>Carrier</th>"
        "<th>Source</th><th class=n>Billed kg</th><th class=n>Days</th><th class=n>Total</th>"
        f"<th class='n hide-sm'>In EUR</th></tr></thead><tbody>{options_rows}</tbody></table></div>"
        if options_rows else ""
    )  # fmt: skip
    drafts = ""
    tools = ""
    open_rfq = is_rfq and not sent and bool(deadline) and datetime.fromisoformat(deadline) > timing.now()
    if mode != "human" and open_rfq:
        tools = _sourcing(ctx, session_id, rfq, evs, deadline, priced=priced, controls=False)
    if mode == "human":
        held = held_drafts(evs)
        if priced:
            drafts = (
                "<h2 id=quote-draft>Review and send the quote</h2><div class=panel>"
                + _draft_form(session_id, None, to=quote.rfq.customer, quote=quote, entered=entered)
                + "</div>"
            )
        if is_rfq and not sent:
            if open_rfq:
                tools = _sourcing(ctx, session_id, rfq, evs, deadline, priced=priced)
                drafts += (
                    f"<details class='panel followup'{' open' if entered and not entered.get('quote_id') else ''}>"
                    "<summary>Write to a carrier</summary><div class='pad muted'>Use Send quote for the first "
                    "customer reply. Use Request carrier bids when you want replies to feed into pricing.</div>"
                    + _draft_form(
                        session_id, None, entered=entered if entered and not entered.get("quote_id") else None
                    )
                    + "</details>"
                )
        else:
            replies = [
                e
                for e in held
                if not e.payload.get("quote") and e.payload.get("purpose") not in ("quote", "fallback_quote")
            ]
            forms = "".join(_draft_form(session_id, e) for e in replies)
            if not forms:
                forms = _draft_form(session_id, None, to=customer, entered=entered)
            drafts += (
                f"<details class='panel followup'{' open' if entered else ''}>"
                "<summary>Write or review a follow-up email</summary>" + forms
            )
            drafts += "</details>"

    rows, minor = zip(*render_events(ctx, evs), strict=True) if evs else ((), ())
    main = (
        _calls_panel(ctx, session_id) + tools + (_quote_panel(quote) if quote or not tools else "") + drafts
        + options_html
        + f"<h2>Activity<span class=count>{len(evs)}</span>"
        + (f"<span class='count muted'>· {minor.count(True)} automatic steps folded</span>" if any(minor) else "")
        + "</h2><div class=panel id=timeline>"
        + group_events(evs, list(rows), list(minor)) + "</div>"
    )  # fmt: skip
    body = (
        f"{head}{notice}{banner}<div class=notice id=session-updated role=status hidden>"
        "New activity arrived while you were editing. Check the timeline before sending.</div>"
        f"<div class=split><div>{main}</div>"
        f"{_details(ctx, session_id, rfq, first, deadline, calls, cost)}</div>"
    )
    last = max(e.id or 0 for e in evs)
    script = (
        "if(location.pathname.endsWith('/send'))history.replaceState(null,'',location.pathname.slice(0,-5));"
        "let editing=Boolean(document.querySelector('[data-edited]')), refreshTimer;"
        # A live background step joins the folded line at the end of the timeline, as `group_events` folds it.
        "function foldEvent(tl,html){const tp=document.createElement('template');tp.innerHTML=html;"
        "const row=tp.content.firstElementChild;let g=tl.lastElementChild;"
        "if(!row.classList.contains('minor')||!g||!g.matches('details.evgroup,.ev.minor')){tl.append(row);return;}"
        "if(!g.matches('details')){const d=document.createElement('details');d.className='evgroup';"
        "d.innerHTML='<summary class=ev><span class=t></span><div></div><span class=\"kind mono\">show all</span></summary>';"
        "d.querySelector('.t').textContent=g.querySelector('.t').textContent;g.replaceWith(d);d.append(g);g=d;}"
        "g.append(row);const n={};for(const w of g.querySelectorAll(':scope>.ev .what')){"
        "const k=w.textContent.toLowerCase();n[k]=(n[k]||0)+1;}const rows=g.querySelectorAll(':scope>.ev').length;"
        "const s=g.querySelector('summary div');s.innerHTML='<span class=what></span> <span class=d></span>';"
        "s.firstChild.textContent=rows+' automatic step'+(rows==1?'':'s');"
        "s.lastChild.textContent='· '+Object.entries(n).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+(v>1?' ×'+v:'')).join(', ');}"
        "document.addEventListener('input',e=>{if(e.target.closest('form[data-preserve]'))editing=true;});"
        "document.querySelector('[data-open-mail]')?.addEventListener('click',e=>{"
        "const detail=document.querySelector(e.currentTarget.hash+' details');if(detail)detail.open=true;});"
        f"live('/events?session_id={session_id}&after_id={last}',e=>{{if(document.getElementById('ev'+e.id))return;"
        "foldEvent(document.getElementById('timeline'),e.html);"
        "if(/^(quote|booking|human|customer|option[.]found|bid[.]received|mail[.]held)/.test(e.kind)){"
        "clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{"
        "if(editing||window.pixelCallLive){document.getElementById('session-updated').hidden=false;}"
        "else{location.reload();}},600);}});"
    )
    return layout(f"Request {session_id}", body, nav="sessions", script=script)


# The Call panel's own styles: rows, the live call bar and the transcript (`voice/call.js` fills them).
CALL_CSS = (
    "#voice-calls .vc-intro{padding:12px 18px;color:var(--muted);border-bottom:1px solid var(--line)}"
    "#voice-calls .vc-row{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line)}"
    "#voice-calls .vc-row:last-of-type{border-bottom:0}"
    "#voice-calls .vc-row>.grow,#voice-calls .vc-live>.grow{flex:1 1 auto;min-width:0}"
    "#voice-calls{margin-bottom:24px}"
    "#voice-calls .btn:disabled{opacity:.45;cursor:default}"
    "#voice-calls .vc-avatar{width:34px;height:34px;border-radius:50%;flex:none;display:grid;place-items:center;"
    "font-weight:600;font-size:12px;color:var(--accent);background:var(--sunk)}"
    "#voice-calls .vc-name{font-weight:500;color:var(--ink);overflow-wrap:anywhere}"
    "#voice-calls .vc-role{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;font-size:11px;"
    "font-weight:500;color:var(--ink-2);background:var(--sunk);vertical-align:1px}"
    "#voice-calls .vc-why{color:var(--muted);margin-top:2px}"
    "#voice-calls .vc-row .btn{gap:6px;margin:0}#voice-calls .vc-row .btn svg{width:14px;height:14px}"
    "#voice-calls .vc-row.on{background:var(--sunk)}"
    "#voice-calls [hidden]{display:none!important}"
    "#voice-calls .vc-live{display:flex;align-items:center;gap:12px;padding:12px 18px;border-top:1px solid var(--line);"
    "background:var(--sunk)}"
    "#voice-calls .vc-dot{width:9px;height:9px;border-radius:50%;background:var(--ok);flex:none;"
    "box-shadow:0 0 0 0 rgba(23,128,79,.5);animation:vc-pulse 1.6s ease-out infinite}"
    "#voice-calls .vc-live.idle .vc-dot{background:var(--line-2);animation:none}"
    "@keyframes vc-pulse{to{box-shadow:0 0 0 8px rgba(23,128,79,0)}}"
    "#voice-calls .vc-timer{font-variant-numeric:tabular-nums;color:var(--muted)}"
    "#voice-calls .vc-hangup{margin:0;border-color:var(--bad);color:var(--bad)}"
    "#voice-calls .vc-status{padding:10px 18px;border-top:1px solid var(--line)}"
    "#voice-calls .vc-status.ok{color:var(--ok)}#voice-calls .vc-status.bad{color:var(--bad)}"
    "#voice-calls .vc-log{display:flex;flex-direction:column;gap:6px;padding:12px 18px;max-height:260px;"
    "overflow-y:auto;border-top:1px solid var(--line)}"
    "#voice-calls .vc-msg{max-width:80%;padding:7px 11px;border-radius:12px;line-height:1.4}"
    "#voice-calls .vc-msg.agent{align-self:flex-start;background:var(--sunk)}"
    "#voice-calls .vc-msg.user{align-self:flex-end;background:var(--accent);color:#fff}"
    "@media (max-width:600px){#voice-calls .vc-row{flex-wrap:wrap}#voice-calls .vc-row .btn{margin-left:46px}}"
)
PHONE = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round' aria-hidden='true'><path d='M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 "
    "19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 "
    "9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z'/></svg>"
)

# The order's agent status (`agent_watch`). Its dot is also the presenter's trigger for a status call
# (`data-order-call`, read by `voice/order_call.js`): it looks like a status light, not a button.
AGENT_CSS = (
    ".ag-watch{display:flex;align-items:center;gap:8px;white-space:nowrap;color:var(--ink-2,var(--ink))}"
    ".ag-dot{all:unset;position:relative;width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none;"
    "cursor:default;box-shadow:0 0 0 6px transparent}"
    ".ag-dot::after{content:'';position:absolute;inset:-3px;border-radius:50%;border:1.5px solid var(--ok);"
    "opacity:0;animation:ag-breathe 3.2s ease-out infinite}"
    "@keyframes ag-breathe{0%{transform:scale(.6);opacity:.55}70%,100%{transform:scale(1.7);opacity:0}}"
    ".ag-dot:focus-visible{outline:none}"
    ".ag-watch.idle .ag-dot{background:var(--line-2)}.ag-watch.idle .ag-dot::after{animation:none}"
    ".ag-watch.calling .ag-dot{background:var(--accent)}.ag-watch.calling .ag-dot::after{border-color:var(--accent);"
    "animation-duration:1.1s}.ag-watch.calling .ag-label{color:var(--accent);font-weight:500}"
    ".ag-sub{color:var(--muted);font-size:12px;margin-top:2px;white-space:nowrap}"
)


def agent_watch(shipment_id: str, status: str) -> str:
    """What the agent does with a booked order: it checks with the carrier on its own while the order moves. The next
    check is spread by the order's id so the rows do not all show the same minute."""
    if status in ("delivered", "cancelled"):
        return (
            f"<div class='ag-watch idle'><button type=button class=ag-dot data-order-call='{esc(shipment_id)}' "
            "tabindex=-1 aria-hidden=true></button><span class=ag-label>Closed</span></div>"
            "<div class=ag-sub>Tracking complete</div>"
        )
    minutes = 7 + sum(map(ord, shipment_id)) % 38
    return (
        f"<div class=ag-watch data-agent-watch='{esc(shipment_id)}'><button type=button class=ag-dot "
        f"data-order-call='{esc(shipment_id)}' tabindex=-1 aria-hidden=true></button>"
        "<span class=ag-label>Agent watching</span></div>"
        f"<div class=ag-sub data-agent-sub>Next carrier check in {minutes} min</div>"
    )


def _calls_panel(ctx: Context, session_id: int) -> str:
    """Call a carrier whose bid is late, the customer our question waits for, or the carrier of a booked order to
    check its live status (`voice/calls.py`)."""
    if not voice.configured(ctx):
        return ""
    targets = voice.targets(ctx, session_id)
    shipment = shipments.for_session(ctx, session_id)
    if not targets and not shipment:
        return ""

    order_row = ""
    if shipment:
        initials = "".join(w[0] for w in shipment.carrier.split()[:2]).upper() or "PX"
        ref = f" · {esc(shipment.booking_reference)}" if shipment.booking_reference else ""
        order_row = (
            f"<div class=vc-row><div class=vc-avatar>{esc(initials)}</div>"
            f"<div class=grow><div><span class=vc-name>{esc(shipment.carrier)}</span>"
            f"<span class=vc-role>Order {esc(shipment.id)}</span></div>"
            f"<div class=vc-why>{esc(shipment.origin)} → {esc(shipment.destination)} · "
            f"{esc(shipment.weight_kg)} kg{ref} · currently {esc(voice.status_label(shipment.status))}</div></div>"
            f"<div>{agent_watch(shipment.id, shipment.status)}</div></div>"
        )

    def row(t: voice.Target) -> str:
        name = names.who(ctx, t.email).name if t.party == "customer" else t.name
        initials = "".join(w[0] for w in name.split()[:2]).upper() or "?"
        role = "Customer" if t.party == "customer" else "Trucker" if t.pickup else "Airline"
        if t.party == "customer":
            why = f"Hasn’t answered our question · needs {esc(', '.join(t.missing) or 'details')}"
        else:
            why = "Pickup price requested · no reply yet" if t.pickup else "Bid requested · no reply yet"
        return (
            f"<div class=vc-row data-row='{esc(t.email)}'><div class=vc-avatar>{esc(initials)}</div>"
            f"<div class=grow><div><span class=vc-name>{esc(name)}</span><span class=vc-role>{role}</span></div>"
            f"<div class=vc-why>{why}</div></div>"
            f"<button type=button class='btn primary' data-call-email='{esc(t.email)}' data-call-name='{esc(name)}'>"
            f"{PHONE}Call</button></div>"
        )

    count = len(targets) + (1 if shipment else 0)
    scripts = "<script type=module src='/voice/call.js'></script>" if targets else ""
    if shipment:
        scripts += "<script type=module src='/voice/order_call.js'></script>"
    return (
        f"<style>{CALL_CSS}{AGENT_CSS}</style>"
        f"<h2 id=calls>Voice agent<span class=count>{count}</span></h2>"
        f"<div class=panel id=voice-calls data-session='{session_id}'>"
        "<div class=vc-intro>PIXEL calls the carrier on its own when an order goes quiet. What it hears updates "
        "this order as the call happens.</div>"
        + order_row
        + "".join(row(t) for t in targets)
        + "<div class=vc-live id=call-live hidden><span class=vc-dot></span>"
        "<div class=grow><b id=call-with></b> <span class=vc-timer id=call-timer>0:00</span></div>"
        "<button type=button class='btn vc-hangup' id=call-hang-up>Hang up</button></div>"
        "<div class=vc-status id=call-status role=status hidden></div>"
        f"<div class=vc-log id=call-log hidden aria-live=polite></div></div>{scripts}"
    )


def _attachments(ctx: Context, session_id: int, message_id: int) -> list[dict]:
    row = ctx.db.execute(
        "SELECT data FROM messages WHERE session_id=? AND id=? AND direction='in'", (session_id, message_id)
    ).fetchone()
    return json.loads(row["data"]).get("attachments", []) if row else []


def _shipment_form(session_id: int, rfq: Rfq | None) -> str:
    def field(label: str, name: str, *attrs: str) -> str:
        value = getattr(rfq, name, "") if rfq else ""
        return f'<label>{label}<input type=text name="{name}" value="{esc(value)}" {" ".join(attrs)}></label>'

    return (
        f'<form class=shipment-form data-preserve method=post action="/sessions/{session_id}/source">'
        '<input type=hidden name=action value=record><div class=shipment-fields>'
        + field("Origin airport", "origin", "maxlength=3")
        + field("Destination airport", "destination", "maxlength=3 required")
        + field("Gross weight (kg)", "weight_kg", "inputmode=decimal required")
        + field("Volume (cbm, if known)", "volume_cbm", "inputmode=decimal")
        + field("Pieces (if known)", "pieces", "inputmode=numeric")
        + field("Pickup postcode", "pickup_postcode", "inputmode=numeric maxlength=5")
        + field("Requested currency code", "currency", "maxlength=3")
        + field("Reply language code", "language", "maxlength=3 required")
        + field("Ready on (YYYY-MM-DD)", "ready_on", "maxlength=10")
        + field("Arrive by (YYYY-MM-DD)", "arrive_by", "maxlength=10")
        + field("Maximum transit days", "max_transit_days", "inputmode=numeric")
        + '<label>Preference<select name=preference required>'
        + "".join(f'<option value={p}{" selected" if rfq and rfq.preference == p else ""}>{p.title()}</option>'
                  for p in ("cheapest", "fastest"))
        + '</select></label><label>Service<select name=door_pickup>'
        + f'<option value=no>Airport to airport</option><option value=yes{" selected" if rfq and rfq.door_pickup else ""}>'
          'Door to port</option></select></label>'
        + '<label>Excluded carriers (comma separated)'
        + f'<input type=text name=excluded_carriers value="{esc(", ".join(rfq.excluded_carriers) if rfq else "")}">'
        + '</label><label>Required airlines (comma separated)'
        + f'<input type=text name=required_carriers value="{esc(", ".join(rfq.required_carriers) if rfq else "")}">'
        + '</label></div><p class=muted>For door service, provide the pickup postcode. Leave origin blank to use its '
          'assigned airport. Enter the reply language as a code such as en, de or fr.</p>'
        + '<button class="btn primary">Save shipment details</button></form>'
    )  # fmt: skip


def _send_form(sid: int, q) -> str:
    """The one-click send of the current quote draft, unchanged, through the draft form's checks (`send`)."""
    return (
        f'<form method=post action="/sessions/{sid}/send" class=handover-actions>'
        f'<input type=hidden name=manual value=1><input type=hidden name=quote_id value="{esc(q.id)}">'
        f'<input type=hidden name=to value="{esc(q.rfq.customer)}">'
        f'<input type=hidden name=subject value="{esc(q.subject)}"><input type=hidden name=body value="{esc(q.body)}">'
        f'<button class="btn primary">Send quote · {money(q.total, q.currency)}</button>'
        '<a href="#quote-draft">Edit the text first</a></form>'
    )


def _step(n: int, state: str, title: str, text: str, controls: str = "") -> str:
    mark = "✓" if state == "done" else str(n)
    return (
        f"<li class='step {state}'><span class=step-n aria-hidden=true>{mark}</span><div class=step-body>"
        f"<b>{title}</b><p>{text}</p>{controls}</div></li>"
    )


def _sourcing(
    ctx: Context, sid: int, rfq: Rfq | None, evs: list[Event], deadline: str, *, priced: bool, controls: bool = True
) -> str:
    """The guide to an open request: request read, prices, quote draft, send. Each step says where it stands. On
    takeover the first step not done carries the primary button, so a person always sees what to click next; on
    autopilot the same steps show how far PIXEL got, without buttons, and offer the takeover."""
    message_id = rfq.message_id if rfq else sid
    mail = next((event for event in evs if event.kind == "mail.in" and event.message_id == message_id), None)
    links = f'<a href="#ev{mail.id}" data-open-mail>Read incoming email</a>' if mail else ""
    links += "".join(
        f'<a href="/sessions/{sid}/mail/{message_id}/attachments/{int(file["id"])}">{esc(file["filename"])}</a>'
        for file in _attachments(ctx, sid, message_id)
    )
    read = operator.usable(rfq)
    found = len(pricing_session.for_request(ctx, rfq)) if read else 0
    asked, answered = ({str(e.payload.get("carrier", "")).casefold() for e in evs if e.kind == k}
                       for k in ("bid.requested", "bid.received"))  # fmt: skip
    waiting = len(asked - answered)

    def btn(action: str, label: str, primary: bool) -> str:
        return f'<button class="btn{" primary" if primary else ""}" name=action value={action}>{label}</button>'

    def form(*buttons: str) -> str:
        if not controls:
            return ""
        return f'<form method=post action="/sessions/{sid}/source" class=handover-actions>{"".join(buttons)}</form>'

    if read:
        facts = [f"<span class=mono>{esc(rfq.origin)} → {esc(rfq.destination)}</span>", f"{esc(rfq.weight_kg)} kg",
                 esc(rfq.preference)] + (["door pickup"] if rfq.door_pickup else [])  # fmt: skip
        facts += [f"leave out {esc(', '.join(rfq.excluded_carriers))}"] if rfq.excluded_carriers else []
        facts += [f"only {esc(', '.join(rfq.required_carriers))}"] if rfq.required_carriers else []
        step1 = _step(1, "done", "Request read", " · ".join(facts))
    elif operator.can_record(evs) and controls:
        step1 = _step(1, "next", "Enter the shipment details", "PIXEL could not read them. Read the email and its "
                      "attachments, then fill in what the price needs.", _shipment_form(sid, rfq))  # fmt: skip
    elif operator.can_record(evs):
        step1 = _step(1, "next", "Request read", "PIXEL could not read the shipment details. Take over to enter them.")
    else:
        step1 = _step(1, "next", "Request read", "PIXEL is still reading the request. Read the email meanwhile.")
    desk = btn("desk", "Ask the rate desk", False) if read and engine._ask_desk(ctx, rfq) else ""
    price_text = f"{found} option{'s' if found != 1 else ''} priced, listed under <a href='#options'>Options</a>."
    if not found:
        price_text = "No price yet. Rate lookups leave the mailbox untouched; bid requests email eligible carriers."
    if waiting:
        price_text += f" Waiting for {waiting} carrier repl{'ies' if waiting != 1 else 'y'}."
    step2 = _step(2, "done" if found else "next" if read else "todo", "Prices", price_text,
                  form(btn("portal", "Find sheet and portal rates", read and not found),
                       btn("bids", "Request carrier bids", False), desk) if read else "")  # fmt: skip
    q = quotes.for_session(ctx, sid)
    if priced and q:
        o = q.option
        draft_text = f"{money(q.total, q.currency)} with {esc(o.carrier)}, {days(o.transit_days)}, via {esc(o.source)}."
        step3 = _step(3, "done", "Quote draft", draft_text, form(btn("draft", "Rebuild from current prices", False)))
        step4 = _step(4, "next", "Send the quote", "Only the first quote in the thread counts. Check the total, "
                      "then send it as drafted or edit the text first.", _send_form(sid, q) if controls else "")  # fmt: skip
    else:
        prepare = form(btn("draft", "Prepare quote draft", True)) if found else ""
        step3 = _step(3, "next" if found else "todo", "Quote draft", "Code picks the best complete price that meets "
                      "the request and writes the mail.", prepare)  # fmt: skip
        step4 = _step(4, "todo", "Send the quote", "Available once a draft is ready.")
    takeover = "" if controls else (
        f"<form method=post action='/sessions/{sid}/takeover' class=handover-actions><button class=btn>Take over"
        "</button><span class=muted>Step in if something looks wrong. You get these steps as buttons; PIXEL holds "
        "its quote for you, and still answers at the deadline if nobody has.</span></form>"
    )  # fmt: skip
    return (
        '<section class="panel pad sourcing" aria-labelledby=sourcing-title>'
        f"<div class=sourcing-head><h2 id=sourcing-title>{'Get the quote out' if controls else 'PIXEL is on it'}</h2>"
        f'<span>Deadline <time datetime="{esc(deadline)}" data-deadline="{esc(deadline)}" class=due>'
        f"{clock(deadline)}</time></span></div>"
        f"<div class=handover-actions>{links}</div><ol class=steps>{step1}{step2}{step3}{step4}</ol>{takeover}</section>"
    )


def _draft_form(session_id: int, held: Event | None, to: str = "", *, quote=None, entered: dict | None = None) -> str:
    p = entered or (held.payload if held else {})
    subject = p.get("subject", quote.subject if quote else "")
    body = p.get("body", quote.body if quote else "")
    form_state = " data-edited" if entered else ""
    draft_id = f'<input type=hidden name=quote_id value="{esc(quote.id)}">' if quote else ""
    body_id = f"mail-body-{held.id if held else quote.id if quote else 'new'}"
    note = (
        "Use the customer’s language. Keep the price, currency, carrier, transit and pickup scope from the draft."
        if quote
        else ""
    )
    return (
        f'<form class=draft data-preserve{form_state} method=post action="/sessions/{session_id}/send">'
        f"<input type=hidden name=manual value=1>{draft_id}"
        f'<label class=field><span>To</span><input type=text inputmode=email name=to value="{esc(p.get("to") or to)}"'
        f"{' readonly' if quote else ''} required></label>"
        f'<label class=field><span>Subject</span><input type=text name=subject value="{esc(subject)}" required></label>'
        f'<div class=mail-text><label for="{esc(body_id)}">Message</label>'
        f'<textarea name=body id="{esc(body_id)}" required>{esc(body)}</textarea></div>'
        f'<div class=actions><span class="muted grow">{note}</span>'
        f'<button class="btn primary">{"Send quote" if quote else "Send email"}</button></div></form>'
    )


def _frame(ctx: Context, ev: Event) -> str:
    data = {"id": ev.id, "kind": ev.kind, "session_id": ev.session_id, "html": render_events(ctx, [ev], new=True)[0][0]}
    return f"id: {ev.id}\ndata: {json.dumps(data)}\n\n"


@router.get("/events")
async def stream(request: Request, session_id: int | None = None, after_id: int = 0):
    """Server-sent events from `core.events.subscribe`, resumable with `after_id` or Last-Event-ID."""
    ctx = ctx_of(request)
    last = int(request.headers.get("last-event-id") or after_id)

    async def frames():
        nonlocal last
        live = events.subscribe(session_id)
        # Subscribe before reading the backlog, so nothing emitted in between is lost; duplicates are skipped by id.
        first = asyncio.ensure_future(anext(live))
        await asyncio.sleep(0)
        try:
            backlog = events.for_session(ctx, session_id, last) if session_id else events.since(ctx, last)
            for ev in backlog:
                last = ev.id
                yield _frame(ctx, ev)
            pending = [await first]
            while True:
                for ev in pending:
                    if (ev.id or 0) > last:
                        last = ev.id or last
                        yield _frame(ctx, ev)
                pending = [await anext(live)]
        except StopAsyncIteration:
            return
        finally:
            first.cancel()
            await live.aclose()

    return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _back(session_id: int, error: str = "", *, result: str = "") -> RedirectResponse:
    params = {key: value for key, value in (("error", error), ("result", result)) if value}
    return RedirectResponse(f"/sessions/{session_id}" + (f"?{urlencode(params)}" if params else ""), 303)


@router.get("/sessions/{session_id}/mail/{message_id}/attachments/{attachment_id}")
async def attachment(request: Request, session_id: int, message_id: int, attachment_id: int):
    ctx = ctx_of(request)
    file = next((file for file in _attachments(ctx, session_id, message_id) if int(file["id"]) == attachment_id), None)
    if file is None:
        raise HTTPException(404, "Attachment not found for this session")
    try:
        data = await ctx.mailbox.attachment(message_id, attachment_id)
    except ApiError as exc:
        events.emit(
            ctx,
            "error",
            "human",
            session_id=session_id,
            message_id=message_id,
            payload={"where": "attachment_download", "status": exc.status},
        )
        raise HTTPException(
            502, "Unable to download the attachment from the mailbox. Try again from the request."
        ) from None
    filename = urlquote(file["filename"], safe="")
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}", "Cache-Control": "no-store"},
    )


@router.post("/sessions/{session_id}/source")
async def source(request: Request, session_id: int):
    ctx = ctx_of(request)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    action = form.get("action", "")
    try:
        result = await operator.source(ctx, session_id, action, form)
    except ValueError as exc:
        return _back(session_id, events.scrub(ctx, str(exc)))
    except TimeoutError:
        result = "Sourcing reached its time limit. Check Options and Activity for any rates or mail already recorded."
    except Exception as exc:  # noqa: BLE001  A broken source must leave the operator and fallback free to continue.
        events.emit(
            ctx,
            "error",
            "human",
            session_id=session_id,
            payload={"where": "manual_sourcing", "error": type(exc).__name__},
        )
        return _back(session_id, "Sourcing failed. Check Activity, then use the rates already available.")
    events.emit(ctx, "human.note", "human", session_id=session_id, payload={"action": action, "result": result})
    return _back(session_id, result=result)


@router.post("/sessions/{session_id}/takeover")
async def takeover(request: Request, session_id: int):
    """mode = human: automatic mail for this session is held as drafts. The deadline fallback still sends."""
    ctx = ctx_of(request)
    ctx.db.execute("UPDATE sessions SET mode = 'human' WHERE id = ?", (session_id,))
    events.emit(ctx, "human.takeover", "human", session_id=session_id)
    return _back(session_id)


@router.post("/sessions/{session_id}/release")
async def release(request: Request, session_id: int):
    ctx = ctx_of(request)
    ctx.db.execute("UPDATE sessions SET mode = 'auto' WHERE id = ?", (session_id,))
    events.emit(ctx, "human.release", "human", session_id=session_id)
    return _back(session_id)


@router.post("/sessions/{session_id}/send")
async def send(request: Request, session_id: int):
    """A person sends a held draft or their own mail through `core.outbox`. While our quote is still a draft,
    what the person sends is that quote, so the first-quote rule and the fallback see it."""
    ctx = ctx_of(request)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    to, subject, body = form.get("to", "").strip(), form.get("subject", "").strip(), form.get("body", "")
    manual = form.get("manual") == "1"
    quote_id = form.get("quote_id")
    if manual and not outbox.taken_over(ctx, session_id):
        return _back(session_id, "Take over the session before sending an email.")

    def invalid(message: str):
        if manual:
            return HTMLResponse(_session_page(ctx, session_id, message, entered=form), status_code=422)
        return _back(session_id, message)

    if not (to and subject and body.strip()):
        return invalid("Enter a recipient, subject and message.")
    quote = quotes.for_session(ctx, session_id)
    draft = quote and quote.sent_at is None and quote.status == QuoteStatus.DRAFT
    if quote_id and (not draft or quote.id != quote_id or outbox.quoted(ctx, session_id)):
        return _back(session_id, "This draft has changed or a quote is already being sent. Review the current session.")
    if manual and not quote_id and not (quote and quote.sent_at):
        first = _mails(ctx, [session_id]).get(session_id, {})
        carrier = (
            to.lower() == RATEDESK
            or ctx.db.execute("SELECT 1 FROM carriers WHERE lower(email)=?", (to.lower(),)).fetchone()
        )
        if first.get("kind") == MailKind.CUSTOMER_RFQ and not carrier:
            return invalid("Choose a known carrier for this email. Send the first customer reply from the quote draft.")
    try:
        if draft and to.lower() == quote.rfq.customer.lower() and (not manual or quote_id):
            valid = compose.check(body, quote.rfq, quote.option) or (
                quote.rfq.language not in compose.TEXT and compose.translated_ok(quote.body, body, quote.option)
            )
            if not valid:
                return invalid("Keep the draft’s total, currency, carrier and transit time in the quote.")
            updated = quote.model_copy(update={"subject": subject, "body": body})
            if manual:
                remaining = (quote.rfq.deadline_at - timing.now()).total_seconds()
                if remaining <= 0:
                    return _back(session_id, "The deadline has passed. Check Activity for the fallback outcome.")
                if quote.option.valid_until and quote.option.valid_until <= timing.now():
                    return _back(session_id, "This rate has expired. Find current rates and rebuild the quote draft.")
                async with asyncio.timeout(remaining):
                    sent = await outbox.send_quote(ctx, updated, operator=True)
            else:
                sent = await outbox.send_quote(ctx, updated, operator=True)
            quotes.save(ctx, sent)
        elif quote_id:
            return _back(session_id, "Use the customer address on the current quote draft.")
        else:
            reply_to = ctx.db.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ? AND direction = 'in' AND sender = ?",
                (session_id, to),
            ).fetchone()[0]
            await outbox.send(
                ctx, to=to, subject=subject, body=body, session_id=session_id,
                in_reply_to=reply_to or session_id, purpose="human",
            )  # fmt: skip
    except outbox.NotSent as e:
        reason = {
            "duplicate_quote": "A quote is already being sent or has been sent. Check Activity.",
            "dry_run": "Mail held because this instance is in dry-run mode.",
            "recipient": "Choose a customer or carrier address known to PIXEL.",
            "superseded": "The customer changed the request. Check the latest incoming email and quote.",
        }.get(e.reason, e.reason)
        return _back(session_id, reason)
    except (ApiError, TimeoutError):
        return invalid("Delivery could not be confirmed. Check Activity and the quote status before retrying.")
    events.emit(
        ctx, "human.note", "human", session_id=session_id, payload={"action": "sent", "to": to, "subject": subject}
    )
    return _back(
        session_id,
        result="Quote sent."
        if draft and (not manual or quote_id) and to.lower() == quote.rfq.customer.lower()
        else "Email sent.",
    )
