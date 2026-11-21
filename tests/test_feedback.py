import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from pixel.config import settings
from pixel.contracts import RateOption, Source
from pixel.core import db, events, feedback
from pixel.core.context import Context
from pixel.crm import quotes
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
    # the verdict appears as soon as we answer; accuracy and time only when the round is scored
    answered = board(100, (4, "accepted"))
    scored = board(D("140.62"), (4, "accepted"))
    scored["requests"][0]["points"] = {"accuracy": 25.62, "time": 15.0}
    ctx = Context(settings(), conn, None, None, None, Board(board(100), answered, scored))
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (7, 'c@x', 'quoted', ?)", (T0,))
    for o in (airline, *pickups):
        emit(ctx, "option.found", "pricing", session_id=7, payload={"option": o.model_dump(mode="json")})
    emit(ctx, "quote.drafted", "rfq", session_id=7, payload={"option": quoted.model_dump(mode="json")})

    assert await feedback.once(ctx) == 0 and feedback.pickup_rule(ctx) == selection.DEFAULT_RULE
    assert await feedback.once(ctx) == 0  # the verdict alone: no points to explain yet
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


async def test_first_poll_and_later_updates_feed_scorer_stats(conn):
    first = board(100, (1, "accepted"), (2, "rejected"))
    updated = board(110, (1, "accepted"), (2, "rejected"))
    updated["requests"][0]["booked"] = True
    ctx = Context(settings(), conn, None, None, None, Board(first, updated, updated))

    assert await feedback.once(ctx) == 0
    assert {row["round"]: row["verdict"] for row in quotes.latest_scores(ctx)} == {1: "accepted", 2: "rejected"}
    stats = quotes.stats(ctx)
    assert (stats["accepted"], stats["rejected"]) == (1, 1)

    assert await feedback.once(ctx) == 0
    assert {row["round"]: row["booked"] for row in quotes.latest_scores(ctx)}[1] is True
    assert await feedback.once(ctx) == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'score'").fetchone()[0] == 3


async def test_board_total_updates_without_request_changes(conn):
    first = board(100, (1, "accepted"))
    first["requests"][0]["cost_points"] = 39.76
    updated = first | {"total": "110.25"}
    ctx = Context(settings(), conn, None, None, None, Board(first, updated, updated))

    await feedback.once(ctx)
    assert quotes.stats(ctx)["points_total"] == 100
    await feedback.once(ctx)
    await feedback.once(ctx)
    stats = quotes.stats(ctx)
    assert stats["points_total"] == D("110.25")
    assert stats["points"] == {"cost": D("39.76")}
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'score.total'").fetchone()[0] == 2


async def test_an_accepted_round_is_being_scored_until_its_row_has_accuracy_and_time(conn):
    def me(total, *rows):
        requests = [{"round_no": n, "verdict": v, "reason": None, "sent_at": T0, "points": p, "total": None,
                     "cost_pending": v == "accepted" and not p.get("cost")} for n, v, p in rows]  # fmt: skip
        return {"total": total, "rank": 6, "scores_revealed": False, "requests": requests}

    one = (1, "accepted", {"accuracy": 45.0, "time": 15.0, "cost": 39.76, "tracking": 10.0})
    ctx = Context(settings(), conn, None, None, None, Board(
        me("109.76", one),
        me("109.76", one, (2, "accepted", {}), (3, "rejected", {})),  # answered, not scored: the total stays
        me("166.26", one, (2, "accepted", {"accuracy": 41.5, "time": 15.0}), (3, "rejected", {})),
    ))  # fmt: skip
    await feedback.once(ctx)
    assert await feedback.once(ctx) == 1  # round 3, rejected: final at once
    rounds = {r["round"]: r for r in quotes.scored_rounds(ctx)}
    assert (rounds[2]["hidden"], rounds[2]["earned"]) == (None, None)  # not 0: being scored
    assert (rounds[3]["hidden"], rounds[3]["earned"]) == (0, 0)
    assert quotes.stats(ctx)["rounds_unknown"] == 1

    assert await feedback.once(ctx) == 1  # round 2, now scored
    rounds = {r["round"]: r for r in quotes.scored_rounds(ctx)}
    assert (rounds[2]["hidden"], rounds[2]["earned"]) == (D("56.5"), D("56.5"))
    s = quotes.stats(ctx)
    assert (s["points_total"], s["points_reported"], s["points_unreported"]) == (D("166.26"), D("166.26"), 0)
    assert (s["rank"], s["rounds_unknown"], s["cost_pending"]) == (6, 0, 1)


async def test_an_accepted_verdict_waits_for_its_points(conn):
    # rounds 27 and 29: the verdict came minutes before its accuracy and time, in the row and in the total alike
    earlier = board(100, (3, "accepted"))
    verdict = board(100, (3, "accepted"), (4, "accepted"))
    scored = board(D("154.32"), (3, "accepted"), (4, "accepted"))
    scored["requests"][1]["points"] = {"accuracy": 39.32, "time": 15.0}
    ctx = Context(settings(), conn, None, None, None, Board(earlier, verdict, verdict, scored))

    assert [await feedback.once(ctx) for _ in range(4)] == [0, 0, 0, 1]
    feedbacks = conn.execute("SELECT payload FROM events WHERE kind = 'feedback'").fetchall()
    assert [json.loads(row["payload"])["hidden"] for row in feedbacks] == ["54.32"]


def _reply_times(ctx, *, include_quote=True):
    requested = datetime.fromisoformat(T0)
    conn = ctx.db
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, customer, state, received_at) VALUES (7, 'c@x', 'open', ?)", (T0,)
    )
    asked, quoted = requested + timedelta(seconds=10), requested + timedelta(seconds=34)
    for message_id, purpose, sent in ((8, "question", asked), (9, "quote", quoted)):
        if purpose == "quote" and not include_quote:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO messages (id, session_id, direction, purpose, sender, recipient, subject, body,"
            " sent_at, data) VALUES (?, 7, 'out', ?, 'us@x', 'c@x', 'Re: RFQ', '', ?, '{}')",
            (message_id, purpose, sent.isoformat()),
        )
    if include_quote:
        # Event time is when the receipt was stored, not when the provider sent the quote.
        events.emit(ctx, "quote.sent", "outbox", session_id=7, message_id=9)
    return asked, quoted


async def test_question_rejection_trips_and_alerts_even_if_the_receipt_arrives_after_the_verdict(conn, monkeypatch):
    monkeypatch.delenv("PIXEL_ASK_MISSING", raising=False)
    result = board(100, (4, "rejected"))
    ctx = Context(settings(), conn, None, None, None, Board(result, result, result))
    asked, _ = _reply_times(ctx, include_quote=False)
    score = result["requests"][0]
    score["answered_at"] = asked.isoformat()
    assert await feedback.once(ctx) == 0
    assert feedback.ask_allowed(ctx)
    _reply_times(ctx)
    assert await feedback.once(ctx) == 1
    assert not feedback.ask_allowed(ctx)
    assert await feedback.once(ctx) == 0
    (issue,) = feedback.attention(conn, 5)
    assert issue["issue"] == "ask_rejected_r4" and issue["issue"] in feedback.REPAIRABLE
    assert issue["detail"]["question_counted_as_quote"] is True


async def test_rejection_at_quote_send_time_does_not_trip_the_circuit_breaker(conn, monkeypatch):
    monkeypatch.delenv("PIXEL_ASK_MISSING", raising=False)
    result = board(100, (4, "rejected"))
    ctx = Context(settings(), conn, None, None, None, Board(board(100), result))
    _, quoted = _reply_times(ctx)
    score = result["requests"][0]
    score.update(answered_at=quoted.isoformat(), reason="price")
    assert await feedback.once(ctx) == 0
    assert await feedback.once(ctx) == 1
    assert not feedback.question_counted_as_quote(ctx, 7, score)
    assert feedback.ask_allowed(ctx)
    assert {i["issue"] for i in feedback.attention(conn, 5)} == {"rejected"}


@pytest.mark.parametrize("reason", sorted(feedback.UNPRICED_REASONS))
def test_unpriced_rejection_requires_an_earlier_outbound_question(conn, reason):
    ctx = Context(settings(), conn, None, None, None, None)
    _, quoted = _reply_times(ctx)
    score = {"verdict": "rejected", "answered_at": None, "reason": reason}
    assert feedback.question_counted_as_quote(ctx, 7, score)
    assert not feedback.question_counted_as_quote(ctx, 7, score | {"verdict": "accepted"})
    conn.execute("UPDATE messages SET sent_at=? WHERE purpose='question'", (quoted.isoformat(),))
    assert not feedback.question_counted_as_quote(ctx, 7, score)


@pytest.mark.parametrize(("seconds_from_question", "found"), [(15, True), (16, False), (-16, False), (20, False)])
def test_question_timing_boundaries(conn, seconds_from_question, found):
    ctx = Context(settings(), conn, None, None, None, None)
    asked, quoted = _reply_times(ctx)
    answered = asked + timedelta(seconds=seconds_from_question)
    score = {"verdict": "rejected", "answered_at": answered.isoformat(), "reason": "price"}
    assert feedback.question_counted_as_quote(ctx, 7, score) is found
    conn.execute(
        "UPDATE messages SET sent_at=? WHERE purpose='question'", ((quoted - timedelta(seconds=5)).isoformat(),)
    )
    assert feedback.question_counted_as_quote(
        ctx, 7, score | {"answered_at": (quoted - timedelta(seconds=5)).isoformat()}
    )
    assert not feedback.question_counted_as_quote(
        ctx, 7, score | {"answered_at": (quoted - timedelta(seconds=4)).isoformat()}
    )
