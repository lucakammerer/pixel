"""CRM viewer: customers, quotes and shipments (with their bookings), read from the CRM tables."""

from urllib.parse import quote as urlquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pixel.contracts import QuoteStatus
from pixel.crm import bookings, customers, quotes, shipments
from pixel.ui.page import (
    customer_link,
    days,
    esc,
    lane,
    layout,
    money,
    page_head,
    search_box,
    stats,
    status,
    tabs,
    when,
)
from pixel.ui.routes import list_sessions, sessions_table

router = APIRouter()

QUOTE_CSS = {QuoteStatus.SENT: "ok", QuoteStatus.ACCEPTED: "ok", QuoteStatus.DECLINED: "", QuoteStatus.EXPIRED: "",
             QuoteStatus.DRAFT: "warn"}  # fmt: skip


def ship_css(state: str) -> str:
    return "bad" if "fail" in state else "warn" if state in ("booking", "requested") else "ok"


def _matches(q: str, *values) -> bool:
    hay = " ".join(str(v) for v in values if v).lower()
    return all(word in hay for word in q.lower().split())


def _frame(section: str, q: str, content: str, counts: dict[str, int]) -> str:
    bar = tabs(
        [
            (f"/crm/{key}", label, key == section, counts.get(key))
            for key, label in (("customers", "Customers"), ("quotes", "Quotes"), ("shipments", "Shipments"))
        ],
        big=True,
    )
    hint = {
        "customers": "Search by address",
        "quotes": "Search customer, lane, carrier, Q- id",
        "shipments": "Search customer, lane, carrier, PX- or BK- reference",
    }[section]
    return (
        page_head("CRM", "Who we work for, what we quoted them and what we booked.")
        + f"<div class=tabrow>{bar}{search_box(q, hint)}</div>"
        + f"<div class=panel>{content}</div>"
    )


def _counts(ctx) -> dict[str, int]:
    db = ctx.db
    return {
        "customers": db.execute("SELECT COUNT(DISTINCT LOWER(customer)) FROM sessions").fetchone()[0],
        "quotes": db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
        "shipments": db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
    }


def _table(head: str, rows: list[str], cols: int, empty: str) -> str:
    body = "".join(rows) or f"<tr><td colspan={cols} class=empty>{empty}</td></tr>"
    return f'<table><thead><tr>{head}</tr></thead><tbody id="refresh" data-live>{body}</tbody></table>'


def _customer_href(email: str) -> str:
    return f"/crm/customers/{urlquote(email.lower(), safe='@')}"


@router.get("/crm")
async def crm_home():
    return RedirectResponse("/crm/customers", 307)


@router.get("/crm/customers", response_class=HTMLResponse)
async def customer_list(request: Request, q: str = ""):
    ctx = request.app.state.ctx
    rows = []
    for c in customers.summary(ctx):
        if q and not _matches(q, c["customer"], *c["lanes"]):
            continue
        top = ", ".join(lane_ for lane_, _ in c["lanes"].most_common(2))
        href = _customer_href(c["customer"])
        rows.append(
            f'<tr data-href="{href}"><td><a class=id href="{href}">{esc(c["customer"])}</a>'
            f"<div class=line2>{esc(top.replace('-', ' → '))}</div></td>"
            f"<td class=n>{c['requests']}</td><td class=n>{c['quoted']}</td><td class=n>{c['won']}</td>"
            f"<td class=n>{c['lost']}</td><td class=n>{c['shipments']}</td>"
            f"<td class=n>{money(c['won_eur'], 'EUR') if c['won'] else '<span class=muted>—</span>'}</td>"
            f"<td class='n hide-sm'>{when(c['last_seen'])}</td></tr>"
        )
    head = (
        "<th>Customer</th><th class=n>Requests</th><th class=n>Quoted</th><th class=n>Won</th><th class=n>Lost</th>"
        "<th class=n>Shipments</th><th class=n>Won value</th><th class='n hide-sm'>Last seen</th>"
    )
    content = _table(head, rows, 8, "No customer yet." if not q else "No customer matches.")
    return layout("Customers", _frame("customers", q, content, _counts(ctx)), nav="crm", script=_live())


@router.get("/crm/quotes", response_class=HTMLResponse)
async def quote_list(request: Request, q: str = ""):
    ctx = request.app.state.ctx
    rows = []
    for x in quotes.recent(ctx):
        o = x.option
        if q and not _matches(
            q, x.id, x.rfq.customer, o.carrier, o.origin, o.destination, f"{o.origin}-{o.destination}"
        ):
            continue
        href = f"/sessions/{x.session_id}"
        response = (
            f"{round((x.sent_at - x.rfq.received_at).total_seconds())} s" if x.sent_at else "<span class=muted>—</span>"
        )
        state = "draft" if x.sent_at is None else str(x.status)
        rows.append(
            f'<tr data-href="{href}"><td class=num>{when(x.created_at)}<div class="line2 mono">{esc(x.id)}</div></td>'
            f"<td>{customer_link(x.rfq.customer)}<div class=line2>{esc(x.rfq.weight_kg)} kg · "
            f"{esc(x.rfq.preference)}</div></td><td>{lane(o.origin, o.destination)}</td>"
            f"<td>{esc(o.carrier)}<div class=line2>via {esc(o.source)} · {days(o.transit_days)}</div></td>"
            f"<td class=n>{money(x.total, x.currency)}</td><td class='n hide-sm'>{response}</td>"
            f"<td>{status(state, QUOTE_CSS.get(x.status, ''))}"
            + (" <span class='tag warn'>fallback</span>" if x.fallback else "")
            + "</td></tr>"
        )
    head = (
        "<th>Created</th><th>Customer</th><th>Lane</th><th>Carrier</th><th class=n>Total</th>"
        "<th class='n hide-sm'>Answered in</th><th>Status</th>"
    )
    content = _table(head, rows, 7, "No quote yet." if not q else "No quote matches.")
    return layout("Quotes", _frame("quotes", q, content, _counts(ctx)), nav="crm", script=_live())


def shipment_rows(ctx, items, q: str = "") -> list[str]:
    by_id = bookings.all_by_id(ctx)
    rows = []
    for s in items:
        b = by_id.get(s.booking_id)
        if q and not _matches(
            q, s.id, s.customer, s.carrier, s.booking_reference, s.origin, s.destination, f"{s.origin}-{s.destination}"
        ):
            continue
        href = f"/sessions/{s.session_id}"
        rows.append(
            f'<tr data-href="{href}"><td><a class="id mono" href="{href}">{esc(s.id)}</a>'
            f"<div class=line2>{when(s.updated_at)}</div></td><td>{customer_link(s.customer)}</td>"
            f"<td>{lane(s.origin, s.destination)}<div class=line2>{esc(s.weight_kg)} kg</div></td>"
            f"<td>{esc(s.carrier)}<div class=line2>{f'via {esc(b.via)}' if b else ''}</div></td>"
            f"<td class=mono>{esc(s.booking_reference or '—')}</td>"
            f"<td class=n>{money(b.price, b.currency) if b and b.price is not None else '—'}</td>"
            f"<td>{status(s.status, ship_css(s.status))}</td></tr>"
        )
    return rows


SHIP_HEAD = (
    "<th>Shipment</th><th>Customer</th><th>Lane</th><th>Carrier</th><th>Airline ref</th><th class=n>Price</th>"
    "<th>Status</th>"
)


@router.get("/crm/shipments", response_class=HTMLResponse)
async def shipment_list(request: Request, q: str = ""):
    ctx = request.app.state.ctx
    rows = shipment_rows(ctx, shipments.recent(ctx, 500), q)
    content = _table(SHIP_HEAD, rows, 7, "No shipment yet." if not q else "No shipment matches.")
    return layout("Shipments", _frame("shipments", q, content, _counts(ctx)), nav="crm", script=_live())


@router.get("/crm/customers/{email}", response_class=HTMLResponse)
async def customer(request: Request, email: str):
    ctx = request.app.state.ctx
    p = customers.profile(ctx, email)
    if p is None:
        raise HTTPException(404, "no such customer")
    win_rate = f"{round(100 * p['won'] / p['quoted'])} %" if p["quoted"] else None
    numbers = stats([
        ("Requests", p["requests"], ""),
        ("Quoted", p["quoted"], ""),
        ("Won", p["won"], f"{p['lost']} lost"),
        ("Win rate", win_rate, "of quotes sent"),
        ("Shipments", len(p["shipments"]), ""),
        ("Won value", money(p["won_eur"], "EUR") if p["won"] else None, "all-in, in EUR"),
    ])  # fmt: skip
    lanes = "".join(f"<span class=tag>{esc(k.replace('-', ' → '))} · {n}</span>" for k, n in p["lanes"].most_common())
    requests = list_sessions(ctx, customer=p["customer"])
    ships = shipment_rows(ctx, p["shipments"])
    body = (
        page_head(esc(p["customer"]), f"Last seen {when(p['last_seen'])}", crumb="<a href='/crm'>CRM</a> / Customers")
        + numbers
        + (f"<h2>Lanes</h2><div class=chips>{lanes}</div>" if lanes else "")
        + f"<h2>Requests<span class=count>{len(requests)}</span></h2>"
        + f"<div class=panel>{sessions_table(requests, show_customer=False)}</div>"
        + f"<h2>Shipments<span class=count>{len(ships)}</span></h2>"
        + f"<div class=panel>{_table(SHIP_HEAD, ships, 7, 'Nothing booked for this customer yet.')}</div>"
    )
    return layout(p["customer"], body, nav="crm", script=_live())


def _live() -> str:
    return "live('/events',refresh());"
