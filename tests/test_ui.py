import json
from decimal import Decimal as D

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

    client.post(
        "/sessions/2/send",
        data={
            "to": "ops@globex.example",
            "subject": "Your quote",
            "body": "Kestrel Airways, 3 days, EUR 370.74 all-in.",
        },
    )
    assert pipe.sent[-1] == {
        "to": "ops@globex.example",
        "subject": "Your quote",
        "body": "Kestrel Airways, 3 days, EUR 370.74 all-in.",
        "purpose": "quote",
    }
    assert quotes.for_session(pipe.ctx, 2).status == QuoteStatus.SENT
    assert "Draft text" not in client.get("/sessions/2").text

    assert pipe.ctx.db.execute("SELECT mode FROM sessions WHERE id = 2").fetchone()[0] == "human"
    client.post("/sessions/2/release")
    assert pipe.ctx.db.execute("SELECT mode FROM sessions WHERE id = 2").fetchone()[0] == "auto"
    kinds = [r[0] for r in pipe.ctx.db.execute("SELECT kind FROM events WHERE session_id = 2 AND kind LIKE 'human.%'")]
    assert kinds == ["human.takeover", "human.note", "human.release"]


def test_dashboard_renders(client, pipe):
    pipe.emit(pipe.ctx, "score.total", "board", payload={"total": "876.54", "rank": 6, "revealed": False})
    pipe.emit(pipe.ctx, "score", "board", payload={"round": 1, "verdict": "accepted", "points": {"cost": 39.76}})
    pipe.emit(
        pipe.ctx, "score", "board", payload={"round": 2, "verdict": "accepted", "points": {}, "cost_pending": True}
    )
    pipe.emit(pipe.ctx, "feedback", "board", payload={"round": 1, "verdict": "accepted", "hidden": "57.10"})
    page = client.get("/dashboard").text
    assert "Spend per accepted quote" in page and "Median response" in page
    assert "876.54" in page and "rank 6" in page
    assert "39.76" in page and "57.10" in page and "96.86" in page  # round 1: cost + accuracy and time
    assert "836.78" in page  # accuracy + time: team total less the reported cost
    assert "next hour" in page and "hidden" in page  # round 2: cost due, accuracy and time not derivable


def test_dashboard_orders_rounds_by_number_after_score_updates(client, pipe):
    for round_no, verdict in [(23, "accepted"), (9, "accepted"), ("21", "accepted"), ("9", "rejected")]:
        pipe.emit(pipe.ctx, "score", "feedback", payload={"round": round_no, "verdict": verdict})

    page = client.get("/dashboard").text
    assert page.index("<tr><td class=num>23</td>") < page.index("<tr><td class=num>21</td>")
    assert page.index("<tr><td class=num>21</td>") < page.index("<tr><td class=num>9</td>")
    assert page.count("<tr><td class=num>9</td>") == 1
    assert "1 rejected" in page


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


def test_dashboard_keeps_tracking_apart_from_the_quote_and_estimates_cost_still_due(client, pipe):
    hour = "2026-11-20T03:"
    pipe.emit(pipe.ctx, "score.total", "board", payload={"total": "239.47", "rank": 6, "revealed": False})
    for n in (21, 22):
        pipe.emit(pipe.ctx, "score", "board", payload={
            "round": n, "verdict": "accepted", "sent_at": f"{hour}0{n - 20}:00+00:00", "cost_pending": True,
            "points": {"accuracy": 45.0, "time": 15.0, "booking": 0.0, "tracking": 10.0}, "total": 70.0})  # fmt: skip
    pipe.emit(pipe.ctx, "score", "board", payload={
        "round": 19, "verdict": "accepted", "sent_at": "2026-11-21T16:50:00+00:00",
        "points": {"accuracy": 45.0, "time": 15.0, "cost": 39.47, "tracking": 0.0}, "total": 99.47})  # fmt: skip
    pipe.ctx.db.execute(  # 0.004 USD for two accepted quotes in the 03:00 hour: 40 × (1 − 0.002 / 0.10) = 39.20
        "INSERT INTO events (ts, kind, actor, payload, cost_usd) VALUES (?, 'llm.call', 'llm', '{}', '0.004')",
        (f"{hour}05:00+00:00",),
    )
    rounds = {r["round"]: r for r in quotes.scored_rounds(pipe.ctx)}
    assert (rounds[21]["quote"], rounds[21]["after"], rounds[21]["earned"]) == (60, 10, 70)
    assert rounds[21]["cost_estimate"] == D("39.20") and rounds[19]["cost_estimate"] is None
    assert rounds[19]["quote"] == D("99.47") and rounds[19]["after"] == 0
    s = quotes.stats(pipe.ctx)
    assert (s["points_quote"], s["points_after"], s["cost_estimate"]) == (D("219.47"), 20, D("78.40"))
    assert s["points_projected"] == D("317.87")
    page = client.get("/dashboard").text
    assert "Tracking + booking" in page and "After the quote" in page and "≈ 39.20" in page and "317.87" in page
