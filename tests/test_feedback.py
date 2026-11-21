import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel.config import settings
from pixel.contracts import RateOption, Source
from pixel.core import db, events, feedback
from pixel.core.context import Context
from pixel.pricing import calc, selection

ROUND = json.loads((Path(__file__).parent / "fixtures" / "feedback-rounds.json").read_text())
T0 = "2026-11-21T15:19:13+00:00"


def option(carrier, eur, days=3, trucker=None, hours=None):
    return RateOption(
        source=Source.BID if trucker else Source.PORTAL, carrier=carrier, origin="CGN", destination="ORD",
        chargeable_kg=D(100), billed_kg=D(100), freight=D(0) if trucker else D(eur), pickup=D(eur) if trucker else D(0),
        trucker=trucker, pickup_hours=hours, total=D(eur), currency="EUR", total_eur=D(eur), transit_days=days,
    )  # fmt: skip


def captured():
    airline = option(ROUND["airline"]["carrier"], ROUND["airline"]["total"], ROUND["airline"]["days"])
    pickups = [option(name, eur, 0, trucker=name, hours=hours) for name, eur, hours in ROUND["pickups"]]
    ours = next(p for p in pickups if p.trucker == ROUND["quoted_trucker"])
    return airline, pickups, calc.with_pickup(airline, ours.total_eur, ours.trucker)


def test_time_points_fall_from_two_minutes_to_six():
    assert [feedback.time_points(s) for s in (30, 120, 240, 360, 400)] == [D(15), D(15), D("7.5"), D(0), D(0)]


def test_lost_points_name_the_trucker_the_scorer_took():
    airline, pickups, quoted = captured()
    found = feedback.explain(D(ROUND["points"]), ROUND["seconds"], quoted, [airline, *pickups])
    assert found["trucker"] == ROUND["scorer_trucker"] and found["carrier"] == airline.carrier
    assert found["best"] == airline.total_eur + D("187.97") and D(19) < found["lost"] < D(20)


def test_full_points_confirm_the_quoted_option_and_unexplained_losses_name_nothing():
    airline, pickups, quoted = captured()
    assert feedback.explain(D(60), 30, quoted, [airline, *pickups])["trucker"] == quoted.trucker
    assert feedback.explain(D(50), 30, quoted, [airline, *pickups])["best"] is None


class Board:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def me(self):
        return self.answers.pop(0)


def board(total, *rounds):
    return {"total": total, "requests": [{"round_no": n, "verdict": v, "reason": None, "sent_at": T0,
                                          "answered_at": "2026-11-21T15:19:47+00:00"} for n, v in rounds]}  # fmt: skip


async def test_a_new_verdict_becomes_feedback_and_teaches_the_pickup_rule(conn, monkeypatch):
    stored = []

    def emit(ctx, kind, actor, *, session_id=None, message_id=None, payload=None, **metrics):
        stored.append((kind, session_id, payload))
        ctx.db.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, ?, ?, ?, ?)",
                       (datetime.now(UTC).isoformat(), session_id, kind, actor, json.dumps(payload or {}, default=str)))  # fmt: skip

    monkeypatch.setattr(events, "emit", emit)
    airline, pickups, quoted = captured()
    # with a tracking bonus in the same poll the total rises by 10 more than the request scored
    ctx = Context(
        settings(), conn, None, None, None, Board(board(100), board(100), board(D("150.62"), (4, "accepted")))
    )
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (7, 'c@x', 'quoted', ?)", (T0,))
    for o in (airline, *pickups):
        emit(ctx, "option.found", "pricing", session_id=7, payload={"option": o.model_dump(mode="json")})
    emit(ctx, "quote.drafted", "rfq", session_id=7, payload={"option": quoted.model_dump(mode="json")})

    assert await feedback.once(ctx) == 0 and feedback.pickup_rule(ctx) == selection.DEFAULT_RULE
    assert await feedback.once(ctx) == 0  # nothing new
    assert await feedback.once(ctx) == 1
    kind, session_id, payload = stored[-1]
    assert (kind, session_id, payload["trucker"], payload["points"]) == (
        "feedback",
        7,
        ROUND["scorer_trucker"],
        "40.62",
    )
    assert json.loads(db.get(conn, feedback.STATE))["verdicts"] == {"4": "accepted"}
    assert feedback.pickup_rule(ctx) == selection.DEFAULT_RULE  # every rule takes this trucker: no lead, no change


async def test_each_kind_of_issue_is_reported_once_per_commit(conn, monkeypatch):
    def emit(ctx, kind, actor, *, session_id=None, message_id=None, payload=None, **metrics):
        ctx.db.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, ?, ?, ?, ?)",
                       (datetime.now(UTC).isoformat(), session_id, kind, actor, json.dumps(payload or {})))  # fmt: skip

    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            return type("Response", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(events, "emit", emit)
    monkeypatch.setattr(feedback.httpx, "AsyncClient", Client)
    ctx = Context(dataclasses.replace(settings(), alert_url="https://hook.test/x"), conn, None, None, None, None)
    emit(ctx, "feedback", "board", session_id=1, payload={"round": 1, "verdict": "accepted", "lost": "0.00"})
    assert await feedback.alert(ctx) == 0
    emit(ctx, "quote.missed", "orchestrator", session_id=2, payload={"reason": "no_price"})
    emit(ctx, "feedback", "board", session_id=3, payload={"round": 3, "verdict": "accepted", "lost": "19.38"})
    emit(ctx, "feedback", "board", session_id=4, payload={"round": 4, "verdict": "rejected", "reason": "price"})
    assert await feedback.alert(ctx) == 3
    assert {i["issue"] for i in calls[0][1]["issues"]} == {"missed", "lost_points", "rejected"}
    emit(ctx, "quote.missed", "orchestrator", session_id=5, payload={"reason": "no_price"})
    assert await feedback.alert(ctx) == 0 and len(calls) == 1  # the same kind, the same commit
    emit(ctx, "human.note", "crm", session_id=6, payload={"note": "Customer reply needs a person"})
    assert await feedback.alert(ctx) == 0  # listed by `attention`, but it costs no points: no repair run
    assert "reply_unhandled" in {i["issue"] for i in feedback.attention(conn, 5)}
    monkeypatch.setattr(feedback, "VERSION", "next")
    monkeypatch.setattr(feedback, "STARTED", datetime.now(UTC).isoformat()[:19] + "Z")  # a restart with new code
    assert await feedback.alert(ctx) == 0  # what happened before the restart is the old code's
    emit(ctx, "quote.missed", "orchestrator", session_id=7, payload={"reason": "no_price"})
    monkeypatch.setattr(feedback, "STARTED", "2026-01-01T00:00:00")
    assert await feedback.alert(ctx) == 3  # the new code missed one too: reported again, with what else is open


def test_one_misread_round_does_not_change_the_pickup_rule(conn):
    ctx = Context(settings(), conn, None, None, None, None)
    for session_id in (1, 2, 3):
        for name, eur, hours in (("A", "100", 8), ("B", "100", 12), ("C", "120", 4)):
            bid = option(name, eur, 0, trucker=name, hours=hours)
            conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', ?, 'option.found', 'p', ?)",
                         (session_id, json.dumps({"option": bid.model_dump(mode="json")}, default=str)))  # fmt: skip
    for session_id in (1, 2):  # the scorer took A, which only min-max picks
        conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', ?, 'feedback', 'b', ?)",
                     (session_id, json.dumps({"trucker": "A"})))  # fmt: skip
        assert feedback.pickup_rule(ctx) == (selection.DEFAULT_RULE if session_id == 1 else "min_max")
