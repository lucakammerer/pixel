import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel.config import settings
from pixel.contracts import RateOption, Source
from pixel.core import db, events, feedback
from pixel.core.context import Context
from pixel.pricing import calc

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

    assert await feedback.once(ctx) == 0 and feedback.pickup_rule(ctx) == "share_of_best"
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
    assert feedback.pickup_rule(ctx) in ("share_of_best", "share_of_worst")  # min-max takes the same trucker here
