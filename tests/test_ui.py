import json
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_crm import T0, quote

from pixel.api.portal import Lane
from pixel.contracts import QuoteStatus
from pixel.core import events, timing
from pixel.crm import quotes, shipments
from pixel.ui import crm, dashboard, home, routes

pytest_plugins = ["test_crm"]  # the `pipe` fixture


@pytest.fixture
def client(pipe):
    app = FastAPI()
    app.include_router(home.router)
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
    page = client.get("/requests", params={"q": q}).text
    assert 'href="/sessions/1"' in page and 'href="/sessions/2"' not in page


def test_list_filter_with_zero_matching_sessions_returns_200(client, pipe):
    # Regression for live 500 KeyError when ?state=tariff or ?state=quoted matches 0 sessions.
    for params, tab_label in (
        ({"state": "tariff"}, "Tariff"),
        ({"state": "quoted", "q": "globex"}, "Quoted"),
        ({"state": "missed"}, "Missed"),
    ):
        resp = client.get("/requests", params=params)
        assert resp.status_code == 200
        assert "No request matches." in resp.text and tab_label in resp.text

    # Empty database: every valid filter tab returns 200 without KeyError.
    pipe.ctx.db.execute("DELETE FROM events")
    for state in ("tariff", "quoted", "open"):
        resp = client.get("/requests", params={"state": state})
        assert resp.status_code == 200 and "No request matches." in resp.text


def test_a_filter_no_session_matches_still_renders(client, pipe):
    """Live 500: ?state=tariff with no matching session raised KeyError on the tab counts."""
    pipe.ctx.db.execute("DELETE FROM events")
    for state in ("tariff", "quoted", "open", "missed"):
        resp = client.get("/requests", params={"state": state})
        assert resp.status_code == 200 and "No request matches." in resp.text


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
    assert "Review and send the quote" in page and "Release to autopilot" in page

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


def test_operator_sources_a_stalled_request_and_sends_only_the_current_draft(client, pipe, monkeypatch):
    monkeypatch.setattr(timing, "now", lambda: T0 + timedelta(seconds=75))
    old = quote(2, customer="ops@globex.example", status=QuoteStatus.DRAFT)
    pipe.ctx.db.execute("UPDATE sessions SET deadline_at=? WHERE id=2", (old.rfq.deadline_at.isoformat(),))
    pipe.emit(pipe.ctx, "quote.waiting", "orchestrator", session_id=2)
    client.post("/sessions/2/takeover")
    assert "Save shipment details" in client.get("/sessions/2").text
    form = {
        "action": "record", "origin": old.rfq.origin, "destination": old.rfq.destination,
        "weight_kg": str(old.rfq.weight_kg), "preference": "cheapest", "language": "en",
        "required_carriers": old.option.carrier, "max_transit_days": str(old.option.transit_days),
    }  # fmt: skip
    assert "Shipment details saved" in client.post("/sessions/2/source", data=form).text
    other = old.option.model_copy(update={"carrier": "Another airline", "carrier_code": None})
    pipe.emit(pipe.ctx, "option.found", "pricing", session_id=2, payload={"option": other.model_dump(mode="json")})
    slow = old.option.model_copy(update={"transit_days": old.option.transit_days + 1})
    pipe.emit(pipe.ctx, "option.found", "pricing", session_id=2, payload={"option": slow.model_dump(mode="json")})
    assert "No complete price yet" in client.post("/sessions/2/source", data={"action": "draft"}).text
    offer = old.option.model_copy(update={"valid_until": old.rfq.deadline_at + timedelta(minutes=5)})
    portal = SimpleNamespace(
        lanes=AsyncMock(return_value=[Lane(offer.origin, offer.destination, ((offer.carrier_code, offer.carrier),))]),
        rate=AsyncMock(return_value=offer), brackets=AsyncMock(return_value=()), rate_remaining=1,
    )  # fmt: skip
    pipe.ctx.portal = portal
    page = client.post("/sessions/2/source", data={"action": "portal"}).text
    assert "Found 0 sheet and 1 portal rates" in page and offer.rate_id in page
    assert portal.rate.await_args.args == (offer.origin, offer.destination, offer.carrier_code, old.rfq.weight_kg, D(0))
    assert pipe.sent == []
    assert "Priced draft ready" in client.post("/sessions/2/source", data={"action": "draft"}).text
    draft = quotes.for_session(pipe.ctx, 2)
    assert draft and draft.option == offer
    send = {"to": draft.rfq.customer, "subject": draft.subject, "body": draft.body, "quote_id": draft.id, "manual": "1"}
    carrier_form = {key: value for key, value in send.items() if key != "quote_id"}
    assert client.post("/sessions/2/send", data=carrier_form | {"to": quote(1).rfq.customer}).status_code == 422
    rejected = client.post("/sessions/2/send", data=send | {"body": "A custom edit with no offer"})
    assert rejected.status_code == 422 and "A custom edit with no offer" in rejected.text and pipe.sent == []
    assert "Quote sent." in client.post("/sessions/2/send", data=send).text
    assert "draft has changed" in client.post("/sessions/2/send", data=send).text
    assert len(pipe.sent) == 1 and pipe.sent[0]["purpose"] == "quote"
    assert "Take over" in client.post("/sessions/2/release").text


def test_dashboard_renders(client, pipe):
    pipe.emit(pipe.ctx, "score.total", "board", payload={"total": "136.53", "rank": 6, "revealed": False})
    pipe.emit(pipe.ctx, "score", "board", payload={
        "round": 1, "verdict": "accepted", "points": {"accuracy": 41.77, "time": 15.0, "cost": 39.76}})  # fmt: skip
    pipe.emit(
        pipe.ctx, "score", "board", payload={"round": 2, "verdict": "accepted", "points": {}, "cost_pending": True}
    )
    page = client.get("/dashboard").text
    assert "Spend / accepted" in page and "Median response" in page
    assert "136.53" in page and "#6" in page and "41.77" in page and "96.53" in page
    assert "next window" in page and "being scored" in page and "together" not in page  # round 2: answered only


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
    # 0.004 USD for two accepted quotes in the window from 03:00: 40 × (1 − 0.002 / 0.10) = 39.20; 03:18 is the next
    pipe.ctx.db.executemany(
        "INSERT INTO events (ts, kind, actor, payload, cost_usd) VALUES (?, 'llm.call', 'llm', '{}', ?)",
        [(f"{hour}05:00+00:00", "0.004"), (f"{hour}18:00+00:00", "0.05")],
    )
    rounds = {r["round"]: r for r in quotes.scored_rounds(pipe.ctx)}
    # the cost still due counts in the round's quote and total; tracking stays apart; booking never scores
    assert (rounds[21]["quote"], rounds[21]["scored"], rounds[21]["earned"]) == (D("99.20"), 70, D("109.20"))
    assert rounds[21]["cost_estimate"] == D("39.20") and rounds[19]["cost_estimate"] is None
    assert rounds[19]["quote"] == rounds[19]["earned"] == D("99.47")
    s = quotes.stats(pipe.ctx)
    assert (s["points_quote"], s["points"]["tracking"], s["cost_estimate"]) == (D("219.47"), 20, D("78.40"))
    assert s["points_rows"] == s["points_total"] + s["cost_estimate"]
    assert s["points_projected"] == D("317.87")
    page = client.get("/dashboard").text
    assert "39.20" in page and "≈" not in page and "109.20" in page and "317.87" in page
    assert "Booking" not in page and "Human writing" in page and "none yet" in page
    assert "= team total 239.47 + cost still due 78.40" in page


async def test_dashboard_shows_our_rank_big_and_the_gap_to_the_teams_around_us(client, pipe):
    from pixel.core import feedback

    class Board:
        async def me(self):
            return {"team": "pixel", "rank": 6, "total": 1851.98, "requests": []}

        async def public(self):
            return {"ranking": [{"rank": n, "name": f"t{n}", "team": f"Team {n}", "total": 2000 - 20 * n} for n in range(1, 6)]
                    + [{"rank": 6, "name": "pixel", "team": "PIXEL", "total": 1851.98},
                       {"rank": 7, "name": "t7", "team": "Team 7", "total": 1700.5}]}  # fmt: skip

    pipe.ctx.board = Board()
    await feedback.once(pipe.ctx)
    await feedback.once(pipe.ctx)  # unchanged: stored once
    assert pipe.ctx.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'score.total'").fetchone()[0] == 1
    page = client.get("/dashboard").text
    assert "<div class=rank>#6<small>of 7</small></div>" in page and "1851.98" in page
    assert "48.02</b> behind #5 Team 5" in page and "151.48</b> ahead of #7 Team 7" in page


def test_timeline_folds_background_steps():
    from pixel.contracts import Event
    from pixel.ui.page import event_row, group_events, is_minor

    kinds = ["mail.in", "route", "option.found", "option.found", "llm.call", "quote.sent", "route", "mail.out"]
    evs = [Event(id=i, ts=T0, kind=k, actor="test", session_id=1) for i, k in enumerate(kinds, 1)]
    html = group_events(evs, [event_row(e) for e in evs], [is_minor(e.kind) for e in evs])
    assert html.count("<details class=evgroup>") == 1  # a lone background step stays a plain row
    assert "4 automatic steps" in html and "option priced ×2, sorted, model call" in html
    assert html.index("Mail received") < html.index("evgroup") < html.index("Quote sent")
    assert all(f'id="ev{e.id}"' in html for e in evs)  # nothing is dropped, only folded
    assert not is_minor("booking.confirmed") and not is_minor("human.takeover") and is_minor("quote.checked")
    assert not is_minor("quote.checked", {"blocking": ["total"]}) and not is_minor("quote.unmet", {"alert": True})


def test_mail_with_a_carrier_is_background():
    from pixel.contracts import Event

    mails = {
        1: {"sender": "Ops@Globex.example", "recipient": "pixel@hackloop.mail"},
        2: {"sender": "pixel@hackloop.mail", "recipient": "desk@airline.example"},
        3: {"sender": "pixel@hackloop.mail", "recipient": "ops@globex.example"},
    }

    def ev(kind, message_id):
        return Event(ts=T0, kind=kind, actor="test", session_id=1, message_id=message_id)

    assert not routes.background(ev("mail.in", 1), mails)  # the customer who opened the thread
    assert routes.background(ev("mail.out", 2), mails)  # a bid request to a carrier
    assert not routes.background(ev("mail.out", 3), mails)  # the quote back to the customer
    assert routes.background(ev("llm.call", None), mails) and not routes.background(ev("quote.sent", None), mails)


def test_takeover_guide_points_at_the_next_step(client, pipe, monkeypatch):
    monkeypatch.setattr(timing, "now", lambda: T0 + timedelta(seconds=75))
    old = quote(2, customer="ops@globex.example", status=QuoteStatus.DRAFT)
    pipe.ctx.db.execute("UPDATE sessions SET deadline_at=? WHERE id=2", (old.rfq.deadline_at.isoformat(),))
    pipe.emit(pipe.ctx, "rfq.extracted", "rfq", session_id=2, message_id=2, payload=old.rfq.model_dump(mode="json"))
    page = client.get("/sessions/2").text  # on autopilot: the same steps, no buttons but the takeover
    assert "PIXEL is on it" in page and "action='/sessions/2/takeover' class=handover-actions" in page
    assert "value=portal" not in page
    client.post("/sessions/2/takeover")
    page = client.get("/sessions/2").text
    assert "<li class='step next'><span class=step-n aria-hidden=true>2</span>" in page  # no price yet
    assert "value=portal" in page and "PIXEL is on it" not in page
    offer = old.option.model_copy(update={"valid_until": old.rfq.deadline_at + timedelta(minutes=5)})
    pipe.emit(pipe.ctx, "option.found", "pricing", session_id=2, payload={"option": offer.model_dump(mode="json")})
    page = client.get("/sessions/2").text
    assert "1 option priced" in page and '<button class="btn primary" name=action value=draft>' in page
    client.post("/sessions/2/source", data={"action": "draft"})
    page = client.get("/sessions/2").text
    assert "Send quote · EUR 370.74" in page and "<span class=step-n aria-hidden=true>4</span>" in page
    assert pipe.sent == []
