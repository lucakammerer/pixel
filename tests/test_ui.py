import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_crm import T0, quote

from pixel.contracts import QuoteStatus
from pixel.core import events
from pixel.crm import quotes, shipments
from pixel.ui import crm, dashboard, routes

pytest_plugins = ["test_crm"]  # the `pipe` fixture


@pytest.fixture
def client(pipe):
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(dashboard.router)
    app.include_router(crm.router)
    app.state.ctx = pipe.ctx
    db = pipe.ctx.db
    for sid, customer in ((1, "buyer@acme.example"), (2, "ops@globex.example")):
        db.execute(
            "INSERT INTO messages (id, session_id, direction, kind, sender, recipient, subject, body, sent_at, data)"
            " VALUES (?, ?, 'in', 'customer_rfq', ?, 'us', ?, 'body', ?, '{}')",
            (sid, sid, customer, f"Air freight request {sid}", T0.isoformat()),
        )
        db.execute("INSERT INTO sessions (id, customer, state, received_at, deadline_at) VALUES (?, ?, 'open', ?, ?)",
                   (sid, customer, T0.isoformat(), T0.isoformat()))  # fmt: skip
        pipe.emit(pipe.ctx, "mail.in", "poller", session_id=sid, message_id=sid)
    quotes.save(pipe.ctx, quote(1))
    s = shipments.create(pipe.ctx, quote(1))
    shipments.save(pipe.ctx, s.model_copy(update={"booking_reference": "BK-AB12CD34"}))
    pipe.emit(
        pipe.ctx, "option.found", "pricing", session_id=1, payload={"option": quote(1).option.model_dump(mode="json")}
    )
    pipe.emit(
        pipe.ctx, "llm.call", "llm", session_id=1, payload={"model": "m", "purpose": "extract"}, cost_usd="0.0004"
    )
    pipe.emit(pipe.ctx, "quote.sent", "rfq", session_id=1)
    return TestClient(app)


@pytest.mark.parametrize("q", ["acme", "FRA-JFK", "fra jfk", "bk-ab12cd34", "Kestrel"])
def test_list_finds_session_by_customer_lane_reference(client, q):
    page = client.get("/", params={"q": q}).text
    assert 'href="/sessions/1"' in page and 'href="/sessions/2"' not in page


def test_session_view_shows_mail_options_and_model_cost(client):
    page = client.get("/sessions/1").text
    assert "Air freight request 1" in page and "Kestrel Airways" in page and "EUR 370.74" in page
    assert "$0.00040" in page and "quote.sent" in page
    assert client.get("/sessions/404").status_code == 404


def test_stream_sends_backlog_then_live(client, pipe, monkeypatch):
    live = pipe.emit(pipe.ctx, "human.note", "human", session_id=1, payload={"note": "hi"})

    async def subscribe(session_id=None):
        yield live  # already sent from the backlog: must not repeat
        yield live.model_copy(update={"id": live.id + 1, "kind": "booking.booked"})

    monkeypatch.setattr(events, "subscribe", subscribe)
    body = client.get("/events", params={"session_id": 1, "after_id": 2}).text
    frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert [f["kind"] for f in frames] == ["option.found", "llm.call", "quote.sent", "human.note", "booking.booked"]
    assert all(f["session_id"] == 1 and 'class="ev' in f["html"] for f in frames)


def test_takeover_edit_send_release(client, pipe):
    quotes.save(pipe.ctx, quote(2, customer="ops@globex.example", status=QuoteStatus.DRAFT))
    assert client.post("/sessions/2/takeover", follow_redirects=False).status_code == 303
    assert pipe.ctx.db.execute("SELECT mode FROM sessions WHERE id = 2").fetchone()[0] == "human"
    pipe.emit(pipe.ctx, "mail.held", "outbox", session_id=2,
              payload={"to": "ops@globex.example", "subject": "Your quote", "body": "Draft text", "reason": "human"})  # fmt: skip
    page = client.get("/sessions/2").text
    assert "Draft text" in page and "Release to autopilot" in page

    client.post("/sessions/2/send", data={"to": "ops@globex.example", "subject": "Your quote", "body": "Edited"})
    assert pipe.sent[-1] == {"to": "ops@globex.example", "subject": "Your quote", "body": "Edited", "purpose": "quote"}
    assert quotes.for_session(pipe.ctx, 2).status == QuoteStatus.SENT
    assert "Draft text" not in client.get("/sessions/2").text

    assert pipe.ctx.db.execute("SELECT mode FROM sessions WHERE id = 2").fetchone()[0] == "human"
    client.post("/sessions/2/release")
    assert pipe.ctx.db.execute("SELECT mode FROM sessions WHERE id = 2").fetchone()[0] == "auto"
    kinds = [r[0] for r in pipe.ctx.db.execute("SELECT kind FROM events WHERE session_id = 2 AND kind LIKE 'human.%'")]
    assert kinds == ["human.takeover", "human.note", "human.release"]


def test_dashboard_renders(client):
    page = client.get("/dashboard").text
    assert "Spend per accepted quote" in page and "Median response" in page


def test_crm_lists_customers_quotes_and_shipments(client):
    assert client.get("/crm", follow_redirects=False).headers["location"] == "/crm/customers"
    customers = client.get("/crm/customers").text
    assert "buyer@acme.example" in customers and "ops@globex.example" in customers
    assert 'href="/crm/customers/buyer@acme.example"' in customers
    assert "ops@globex.example" not in client.get("/crm/customers", params={"q": "acme"}).text
    quotes_page = client.get("/crm/quotes").text
    assert "Q-1" in quotes_page and "EUR 370.74" in quotes_page and "Kestrel Airways" in quotes_page
    shipments_page = client.get("/crm/shipments").text
    assert "BK-AB12CD34" in shipments_page and 'href="/sessions/1"' in shipments_page
    assert "BK-AB12CD34" not in client.get("/crm/shipments", params={"q": "globex"}).text


def test_customer_profile_shows_requests_and_shipments(client):
    page = client.get("/crm/customers/Buyer@Acme.example").text
    assert "Air freight request 1" in page and "BK-AB12CD34" in page and "FRA → JFK" in page
    assert "Air freight request 2" not in page
    assert client.get("/crm/customers/nobody@example.com").status_code == 404
