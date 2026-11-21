"""Sessions UI: find a session, watch it live, step in. Reads the event log only."""

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from pixel.agents.rfq import compose
from pixel.contracts import Event, QuoteStatus
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.ui.page import (
    clock,
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
        item = {
            "id": row["id"], "first_ts": row["first_ts"], "last_ts": row["last_ts"], "cost": row["cost"] or 0,
            "customer": (quote.rfq.customer if quote else row["sender"]) or "", "subject": row["subject"] or "",
            "lane": lane_text, "quote": quote, "shipment": shipment, "status": label, "css": css,
            "mode": row["mode"] or "auto", "deadline": row["deadline_at"] if label == "open" else None,
        }  # fmt: skip
        haystack = " ".join(
            str(x) for x in (
                item["id"], item["customer"], item["subject"], lane_text, lane_text.replace(" → ", "-"), label,
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
                f"<a class=id href='{href}'>{esc(s['customer'])}</a><div class=line2>{esc(s['subject'])[:90]}</div>"
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


@router.get("/", response_class=HTMLResponse)
async def sessions(request: Request, q: str = "", state: str = ""):
    """The list: newest first, searchable by customer, lane, reference; time left on open requests."""
    ctx = ctx_of(request)
    matching = list_sessions(ctx, q)
    counts: dict[str, int] = {}
    for s in matching:
        key = s["status"].split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
    items = [s for s in matching if not state or s["status"].split(" ")[0] == state]

    def href(value: str) -> str:
        params = {k: v for k, v in (("q", q), ("state", value)) if v}
        return "/" + (f"?{urlencode(params)}" if params else "")

    bar = tabs(
        [(href(""), "All", not state, len(matching))]
        + [(href(f), f.capitalize(), state == f, counts.get(f, 0)) for f in FILTERS if counts.get(f) or state == f],
        live_id="tabs",
    )
    body = (
        page_head("Requests", "Every customer thread, newest first. Open requests show the time left.")
        + f"<div class=toolbar>{bar}{search_box(q, 'Search customer, lane, reference')}</div>"
        + f"<div class=panel>{sessions_table(items)}</div>"
    )
    return layout("Requests", body, nav="sessions", script="live('/events',refresh());")


def _mails(ctx: Context, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = ctx.db.execute(f"SELECT id, sender, recipient, subject, body FROM messages WHERE id IN ({marks})", ids)
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


def _details(ctx: Context, session_id: int, quote, first: dict, deadline, calls: int, cost) -> str:
    customer = quote.rfq.customer if quote else first.get("sender", "")
    facts = [("Customer", customer_link(customer))]
    if quote:
        r = quote.rfq
        facts += [
            ("Lane", lane(r.origin, r.destination)),
            ("Weight", f"{esc(r.weight_kg)} kg" + (f" · {esc(r.volume_cbm)} cbm" if r.volume_cbm else "")),
            ("Preference", esc(r.preference)),
            ("Pickup", f"door · {esc(r.pickup_postcode or '')}" if r.door_pickup else "airport to airport"),
            ("Received", clock(r.received_at)),
        ]
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
async def session(request: Request, session_id: int, error: str = ""):
    """One session: mails, decisions, options with their prices, model calls with cost, the quote."""
    ctx = ctx_of(request)
    evs = events.for_session(ctx, session_id)
    if not evs:
        raise HTTPException(404, "no such session")
    row = ctx.db.execute("SELECT mode, deadline_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    mode = row["mode"] if row else "auto"
    quote = quotes.for_session(ctx, session_id)
    first = _mails(ctx, [session_id]).get(session_id, {})
    label, css = status_of({e.kind for e in evs})
    cost = sum(e.cost_usd for e in evs)
    calls = sum(1 for e in evs if e.kind == "llm.call")
    deadline = row["deadline_at"] if row and label == "open" else None

    if mode == "human":
        right = status(label, css)
        notice = (
            "<div class='notice human'><div class=grow><b>You are handling this request.</b> Automatic mail is held "
            "as drafts below; the deadline fallback still sends a quote.</div>"
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
        title, f"#{session_id} · {customer_link(customer)}", crumb="<a href='/'>Requests</a>",
        right=f"<div style='display:flex;gap:14px;align-items:center'>{right}</div>",
    )  # fmt: skip
    banner = f"<div class='notice bad'>Not sent: {esc(error)}</div>" if error else ""

    options = [e.payload.get("option", e.payload) for e in evs if e.kind == "option.found"]
    chosen = (quote.option.carrier, str(quote.option.total)) if quote else None
    options_rows = "".join(
        f"<tr class='{'chosen' if chosen == (o.get('carrier'), str(o.get('total'))) else ''}'>"
        f"<td>{esc(o.get('carrier'))}"
        + (" <span class=tag>quoted</span>" if chosen == (o.get("carrier"), str(o.get("total"))) else "")
        + f"</td><td class=muted>{esc(o.get('source'))}</td><td class=n>{esc(o.get('billed_kg'))}</td>"
        f"<td class=n>{esc(o.get('transit_days'))}</td><td class=n>{money(o.get('total'), o.get('currency'))}</td>"
        f"<td class='n hide-sm'>{money(o.get('total_eur'), 'EUR')}</td></tr>"
        for o in sorted(options, key=lambda o: float(o.get("total_eur") or o.get("total") or 0))
    )
    options_html = (
        f"<h2>Options<span class=count>{len(options)}</span></h2><div class=panel><table><thead><tr><th>Carrier</th>"
        "<th>Source</th><th class=n>Billed kg</th><th class=n>Days</th><th class=n>Total</th>"
        f"<th class='n hide-sm'>In EUR</th></tr></thead><tbody>{options_rows}</tbody></table></div>"
        if options_rows else ""
    )  # fmt: skip
    drafts = "".join(_draft_form(session_id, e, quote) for e in held_drafts(evs))
    if mode == "human" and not drafts:
        drafts = _draft_form(session_id, None, quote, to=quote.rfq.customer if quote else first.get("sender", ""))

    main = (
        (f"<h2 style='margin-top:0'>Drafts to send</h2><div class=panel>{drafts}</div><div style='height:8px'></div>"
         if drafts else "")
        + _quote_panel(quote) + options_html
        + f'<h2>Activity<span class=count>{len(evs)}</span></h2><div class=panel id=timeline>'
        + "".join(render_events(ctx, evs)) + "</div>"
    )  # fmt: skip
    body = (
        f"{banner}{head}{notice}<div class=split><div>{main}</div>"
        f"{_details(ctx, session_id, quote, first, deadline, calls, cost)}</div>"
    )
    last = max(e.id or 0 for e in evs)
    script = (
        f"live('/events?session_id={session_id}&after_id={last}',e=>{{if(document.getElementById('ev'+e.id))return;"
        "document.getElementById('timeline').insertAdjacentHTML('beforeend',e.html);"
        "if(/^(quote|booking|human|customer|mail.held)/.test(e.kind))setTimeout(()=>location.reload(),600);});"
    )
    return layout(f"Request {session_id}", body, nav="sessions", script=script)


def _draft_form(session_id: int, held: Event | None, quote, to: str = "") -> str:
    p = held.payload if held else {}
    is_quote = quote is not None and quote.sent_at is None and quote.status == QuoteStatus.DRAFT
    if is_quote and (not held or "quote" in p):  # a held quote names only its id; the text is in the CRM
        to = quote.rfq.customer
    subject = p.get("subject") or (quote.subject if is_quote else "")
    text = p.get("body") or (quote.body if is_quote else "")
    note = "This is the quote. Only the first quote in a thread counts." if is_quote else ""
    return (
        f'<form class=draft method=post action="/sessions/{session_id}/send">'
        f'<div class=field><label>To</label><input type=text name=to value="{esc(p.get("to") or to)}"></div>'
        f'<div class=field><label>Subject</label><input type=text name=subject value="{esc(subject)}"></div>'
        f"<textarea name=body>{esc(text)}</textarea>"
        f'<div class=actions><span class="muted grow">{note}</span><button class="btn primary">Send</button></div></form>'
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


def _back(session_id: int, error: str = "") -> RedirectResponse:
    return RedirectResponse(f"/sessions/{session_id}" + (f"?error={quote_plus(error)}" if error else ""), 303)


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
    if not (to and subject and body.strip()):
        return _back(session_id, "to, subject and text are all needed")
    quote = quotes.for_session(ctx, session_id)
    try:
        if (
            quote
            and quote.sent_at is None
            and quote.status == QuoteStatus.DRAFT
            and to.lower() == quote.rfq.customer.lower()
        ):
            if not compose.check(body, quote.rfq, quote.option):
                return _back(session_id, "Keep the quote total, currency, carrier and transit time in the first reply.")
            sent = await outbox.send_quote(
                ctx, quote.model_copy(update={"subject": subject, "body": body}), operator=True
            )
            quotes.save(ctx, sent)
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
        return _back(session_id, e.reason)
    events.emit(
        ctx, "human.note", "human", session_id=session_id, payload={"action": "sent", "to": to, "subject": subject}
    )
    return _back(session_id)
