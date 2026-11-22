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
from pixel.ui import names, operator
from pixel.ui.page import (
    clock,
    company,
    customer_link,
    days,
    esc,
    event_row,
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


def render_events(ctx: Context, evs: list[Event], *, new: bool = False) -> list[str]:
    mails = _mails(ctx, [e.message_id for e in evs if e.message_id and e.kind in ("mail.in", "mail.out")])
    return [event_row(e, mails.get(e.message_id) if e.kind in ("mail.in", "mail.out") else None, new=new) for e in evs]


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
            rows += [("Shipment", f"<span class=mono>{esc(shipment.id)}</span>"), ("Status", esc(shipment.status))]
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
            "<div class='notice human handover'><div class=grow><b>You’re handling this session</b>"
            "<p>Most automatic replies are held as drafts. Carrier sourcing can continue, "
            "and the deadline fallback can still send.</p></div>"
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
    if mode == "human":
        held = held_drafts(evs)
        if priced:
            drafts = (
                "<h2 id=quote-draft>Review and send the quote</h2><div class=panel>"
                + _draft_form(session_id, None, to=quote.rfq.customer, quote=quote, entered=entered)
                + "</div>"
            )
        if is_rfq and not sent:
            if deadline and datetime.fromisoformat(deadline) > timing.now():
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

    main = (
        _calls_panel(ctx, session_id) + tools + _quote_panel(quote) + drafts + options_html
        + f'<h2>Activity<span class=count>{len(evs)}</span></h2><div class=panel id=timeline>'
        + "".join(render_events(ctx, evs)) + "</div>"
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
        "document.addEventListener('input',e=>{if(e.target.closest('form[data-preserve]'))editing=true;});"
        "document.querySelector('[data-open-mail]')?.addEventListener('click',e=>{"
        "const detail=document.querySelector(e.currentTarget.hash+' details');if(detail)detail.open=true;});"
        f"live('/events?session_id={session_id}&after_id={last}',e=>{{if(document.getElementById('ev'+e.id))return;"
        "document.getElementById('timeline').insertAdjacentHTML('beforeend',e.html);"
        "if(/^(quote|booking|human|customer|option[.]found|bid[.]received|mail[.]held)/.test(e.kind)){"
        "clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{"
        "if(editing||window.pixelCallLive){document.getElementById('session-updated').hidden=false;}"
        "else{location.reload();}},600);}});"
    )
    return layout(f"Request {session_id}", body, nav="sessions", script=script)


def _calls_panel(ctx: Context, session_id: int) -> str:
    """Call a carrier whose bid is late or the customer our question waits for (`voice/calls.py`). What they say
    reaches the thread as an answer by mail would; shown only while it can still change the quote."""
    targets = voice.targets(ctx, session_id) if voice.configured(ctx) else []
    if not targets:
        return ""
    rows = "".join(
        f"<div class='pad' style='display:flex;gap:12px;align-items:center'><div class=grow><b>{esc(t.name)}</b>"
        f"<div class=muted>{'Customer: our question is unanswered' if t.party == 'customer' else 'Pickup bid' if t.pickup else 'Airline bid'}"
        f"{' · ' + esc(', '.join(t.missing)) if t.missing else ' · not answered yet'}</div></div>"
        f"<button type=button class=btn data-call-email='{esc(t.email)}' data-call-name='{esc(t.name)}'>Call</button></div>"
        for t in targets
    )  # fmt: skip
    return (
        f"<h2 id=calls>Call<span class=count>{len(targets)}</span></h2>"
        f"<div class=panel id=voice-calls data-session='{session_id}'>{rows}"
        "<div class=pad><div id=call-status role=status hidden></div>"
        "<button type=button class=btn id=call-hang-up hidden>Hang up</button></div><div id=call-log></div></div>"
        "<script type=module src='/voice/call.js'></script>"
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


def _sourcing(ctx: Context, sid: int, rfq: Rfq | None, evs: list[Event], deadline: str, *, priced: bool) -> str:
    message_id = rfq.message_id if rfq else sid
    mail = next((event for event in evs if event.kind == "mail.in" and event.message_id == message_id), None)
    links = f'<a href="#ev{mail.id}" data-open-mail>Read incoming email</a>' if mail else ""
    if priced:
        links += '<a href="#quote-draft">Review quote draft</a>'
    links += "".join(
        f'<a href="/sessions/{sid}/mail/{message_id}/attachments/{int(file["id"])}">{esc(file["filename"])}</a>'
        for file in _attachments(ctx, sid, message_id)
    )
    if operator.usable(rfq):
        controls = (
            f'<form method=post action="/sessions/{sid}/source" class=handover-actions>'
            '<button class=btn name=action value=portal>Find sheet and portal rates</button>'
            '<button class=btn name=action value=bids>Request carrier bids</button>'
            + ('<button class=btn name=action value=desk>Ask the rate desk</button>'
               if engine._ask_desk(ctx, rfq) else "")
            + f'<button class="btn{" " if priced else " primary"}" name=action value=draft>'
              f'{"Rebuild" if priced else "Prepare"} quote draft</button></form>'
            '<p class=muted>Rate lookups leave the mailbox untouched. Bid requests email eligible airlines and '
            'truckers. A draft uses the best complete price available for this request.</p>'
        )  # fmt: skip
    elif operator.can_record(evs):
        controls = "<p>Read the email and attachments, then enter the shipment details before sourcing.</p>"
        controls += _shipment_form(sid, rfq)
    else:
        controls = "<p>Waiting for shipment details. Read the incoming request while PIXEL finishes its first pass.</p>"
    return (
        '<section class="panel pad sourcing" aria-labelledby=sourcing-title>'
        "<div class=sourcing-head><h2 id=sourcing-title>Source a quote</h2>"
        f'<span>Deadline <time datetime="{esc(deadline)}" data-deadline="{esc(deadline)}" class=due>'
        f"{clock(deadline)}</time></span></div>"
        f"<div class=handover-actions>{links}</div>{controls}</section>"
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
    data = {"id": ev.id, "kind": ev.kind, "session_id": ev.session_id, "html": render_events(ctx, [ev], new=True)[0]}
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
