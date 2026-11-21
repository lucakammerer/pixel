"""Sessions UI: find a session, watch it live, step in. Reads the event log only."""

import asyncio
import json
from urllib.parse import parse_qs, quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from pixel.agents.rfq import compose
from pixel.contracts import Event, QuoteStatus
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.crm import bookings, quotes, shipments
from pixel.ui.page import clock, esc, event_row, layout, money

router = APIRouter()

STATUS = (  # first match wins: (event kind, label, css class)
    ("booking.confirmed", "confirmed", "ok"), ("booking.booked", "booked", "ok"),
    ("booking.failed", "booking failed", "bad"), ("customer.declined", "declined", "muted"),
    ("customer.accepted", "accepted", "ok"), ("quote.missed", "missed", "bad"),
    ("quote.fallback", "quoted · fallback", "warn"), ("quote.sent", "quoted", "ok"),
    ("tracking.answered", "tracking", "muted"), ("tariff.ingested", "tariff", "muted"),
    ("tariff.failed", "tariff failed", "bad"),
)  # fmt: skip


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


def status_of(kinds: set[str]) -> tuple[str, str]:
    for kind, label, css in STATUS:
        if kind in kinds:
            return label, css
    return "open", "warn"


def list_sessions(ctx: Context, q: str = "", state: str = "", limit: int = 300) -> list[dict]:
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
        quote = quotes.for_session(ctx, row["id"])
        shipment = shipments.for_session(ctx, row["id"])
        label, css = status_of(set((row["kinds"] or "").split(",")))
        lane = f"{quote.option.origin} → {quote.option.destination}" if quote else ""
        item = {
            "id": row["id"], "first_ts": row["first_ts"], "last_ts": row["last_ts"], "cost": row["cost"] or 0,
            "customer": (quote.rfq.customer if quote else row["sender"]) or "", "subject": row["subject"] or "",
            "lane": lane, "quote": quote, "shipment": shipment, "status": label, "css": css,
            "mode": row["mode"] or "auto", "deadline": row["deadline_at"] if label == "open" else None,
        }  # fmt: skip
        haystack = " ".join(
            str(x) for x in (
                item["id"], item["customer"], item["subject"], lane, lane.replace(" → ", "-"), label,
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


def sessions_table(items: list[dict]) -> str:
    if not items:
        return '<tbody id="refresh"><tr><td colspan=7 class=muted>No session matches.</td></tr></tbody>'
    rows = []
    for s in items:
        quote, shipment = s["quote"], s["shipment"]
        when = f'<span data-deadline="{esc(s["deadline"])}" class=pill></span>' if s["deadline"] else ""
        mode = ' <span class="pill k-human">person</span>' if s["mode"] == "human" else ""
        refs = " ".join(esc(r) for r in (shipment and shipment.id, shipment and shipment.booking_reference) if r)
        rows.append(
            f'<tr><td class=mono><a href="/sessions/{s["id"]}">{s["id"]}</a></td><td>{clock(s["first_ts"])}</td>'
            f"<td>{esc(s['customer'])}<br><span class=muted>{esc(s['subject'])[:80]}</span></td><td>{esc(s['lane'])}</td>"
            f"<td class=n>{money(quote.total, quote.currency) if quote else '—'}</td>"
            f'<td><span class="pill {s["css"]}">{esc(s["status"])}</span>{mode} {when}</td>'
            f"<td class=mono>{refs}</td></tr>"
        )
    return f'<tbody id="refresh">{"".join(rows)}</tbody>'


@router.get("/", response_class=HTMLResponse)
async def sessions(request: Request, q: str = "", state: str = ""):
    """The list: newest first, searchable by customer, lane, reference; time left on open requests."""
    items = list_sessions(ctx_of(request), q, state)
    states = "".join(
        f"<option {'selected' if state == s else ''}>{s}</option>"
        for s in ("", "open", "quoted", "accepted", "booked", "confirmed", "missed", "declined", "tracking")
    )
    body = (
        '<form class=search><input type=search name=q autofocus placeholder="Customer, lane (FRA-JFK), PX-… or BK-… '
        f'reference, carrier" value="{esc(q)}"><select name=state>{states}</select><button>Search</button></form>'
        "<table><thead><tr><th>Session</th><th>Received</th><th>Customer</th><th>Lane</th><th class=n>Quote</th>"
        f"<th>Status</th><th>References</th></tr></thead>{sessions_table(items)}</table>"
    )
    return layout("Sessions", body, nav="sessions", script="live('/events',refresh());")


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


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session(request: Request, session_id: int, error: str = ""):
    """One session: mails, decisions, options with their prices, model calls with cost, the quote."""
    ctx = ctx_of(request)
    evs = events.for_session(ctx, session_id)
    if not evs:
        raise HTTPException(404, "no such session")
    row = ctx.db.execute("SELECT mode, deadline_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    mode = row["mode"] if row else "auto"
    quote, shipment = quotes.for_session(ctx, session_id), shipments.for_session(ctx, session_id)
    booking = bookings.for_session(ctx, session_id)
    first = _mails(ctx, [session_id]).get(session_id, {})
    label, css = status_of({e.kind for e in evs})
    cost = sum(e.cost_usd for e in evs)
    calls = sum(1 for e in evs if e.kind == "llm.call")
    deadline = row["deadline_at"] if row and label == "open" else None

    step = (
        f'<form method=post action="/sessions/{session_id}/release"><button class=ghost>Release to autopilot</button></form>'
        if mode == "human"
        else f'<form method=post action="/sessions/{session_id}/takeover"><button>Take over</button></form>'
    )
    head = (
        f"<div class=row><div><h1>{esc(first.get('subject') or f'Session {session_id}')}</h1>"
        f"<div class=muted>Session {session_id} · {esc(quote.rfq.customer if quote else first.get('sender', ''))}</div></div>"
        f'<span class="pill {css}">{esc(label)}</span>'
        + (f'<span data-deadline="{esc(deadline)}" class=pill></span>' if deadline else "")
        + ('<span class="pill k-human">a person is in charge</span>' if mode == "human" else "")
        + f"<span class=spacer></span>{step}</div>"
    )
    banner = f"<div class=banner>Not sent: {esc(error)}</div>" if error else ""

    facts = [("Model calls", f"{calls} · ${float(cost):.5f}")]
    if quote:
        o = quote.option
        facts = [
            ("Request", f"{esc(o.origin)} → {esc(o.destination)} · {esc(quote.rfq.weight_kg)} kg"
             + (f" · {esc(quote.rfq.volume_cbm)} cbm" if quote.rfq.volume_cbm else "")
             + f" · {esc(quote.rfq.preference)}" + (" · door pickup" if quote.rfq.door_pickup else "")),
            ("Quote", f"<b>{money(quote.total, quote.currency)}</b> · {esc(quote.status)}"
             + (' · <span class="warn">fallback</span>' if quote.fallback else "")),
            ("Option", f"{esc(o.source)} · {esc(o.carrier)} · {o.transit_days} days · billed {esc(o.billed_kg)} kg"),
            ("Breakdown", f"freight {money(o.freight)} · fuel {money(o.fuel)} · security {money(o.security)}"
             + (f" · pickup {money(o.pickup)}" if o.pickup else "")),
        ] + facts  # fmt: skip
    if shipment:
        facts.append(("Shipment", f"<span class=mono>{esc(shipment.id)}</span> · {esc(shipment.status)}"))
    if booking:
        reference = esc(booking.booking_reference or "—")
        facts.append(
            ("Booking", f"via {esc(booking.via)} · <span class=mono>{reference}</span> · {esc(booking.status)}")
        )
    summary = "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>"

    options = [e.payload.get("option", e.payload) for e in evs if e.kind == "option.found"]
    options_html = "".join(
        f"<tr><td>{esc(o.get('source'))}</td><td>{esc(o.get('carrier'))}</td><td class=n>{esc(o.get('billed_kg'))}</td>"
        f"<td class=n>{esc(o.get('transit_days'))}</td><td class=n>{money(o.get('total'), o.get('currency'))}</td>"
        f"<td class=n>{money(o.get('total_eur'), 'EUR')}</td></tr>"
        for o in sorted(options, key=lambda o: float(o.get("total_eur") or o.get("total") or 0))
    )
    drafts = "".join(_draft_form(session_id, e, quote) for e in held_drafts(evs))
    if mode == "human" and not drafts:
        drafts = _draft_form(session_id, None, quote, to=quote.rfq.customer if quote else first.get("sender", ""))

    body = (
        f"{banner}{head}<div class=cols style='margin-top:14px'><div class=card>{summary}</div><div>"
        + (f"<table><thead><tr><th>Source</th><th>Carrier</th><th class=n>Billed kg</th><th class=n>Days</th>"
           f"<th class=n>Total</th><th class=n>EUR</th></tr></thead><tbody>{options_html}</tbody></table>"
           if options_html else '<div class="card muted">No priced option yet.</div>')
        + "</div></div>"
        + (f"<h2>Drafts to send</h2>{drafts}" if drafts else "")
        + f'<h2>Timeline</h2><div class=card style="padding:0" id=timeline>{"".join(render_events(ctx, evs))}</div>'
    )  # fmt: skip
    last = max(e.id or 0 for e in evs)
    script = (
        f"live('/events?session_id={session_id}&after_id={last}',e=>{{if(document.getElementById('ev'+e.id))return;"
        "document.getElementById('timeline').insertAdjacentHTML('beforeend',e.html);"
        "if(/^(quote|booking|human|customer|mail.held)/.test(e.kind))setTimeout(()=>location.reload(),600);});"
    )
    return layout(f"Session {session_id}", body, nav="sessions", script=script)


def _draft_form(session_id: int, held: Event | None, quote, to: str = "") -> str:
    p = held.payload if held else {}
    is_quote = quote is not None and quote.sent_at is None and quote.status == QuoteStatus.DRAFT
    if is_quote and (not held or "quote" in p):  # a held quote names only its id; the text is in the CRM
        to = quote.rfq.customer
    subject = p.get("subject") or (quote.subject if is_quote else "")
    text = p.get("body") or (quote.body if is_quote else "")
    note = "Sending this is the quote: the first one in the thread counts." if is_quote else ""
    return (
        f'<form class=card method=post action="/sessions/{session_id}/send" style="margin-bottom:10px">'
        f'<div class=row><label class=muted>To</label><input type=text name=to value="{esc(p.get("to") or to)}"></div>'
        f'<div class=row style="margin:8px 0"><label class=muted>Subject</label>'
        f'<input type=text name=subject value="{esc(subject)}"></div><textarea name=body>{esc(text)}</textarea>'
        f'<div class=row style="margin-top:8px"><span class="muted">{note}</span><span class=spacer></span>'
        "<button>Send</button></div></form>"
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
