"""The pickup price memory (`pricing/pickup.py` `estimates`) on captured trucker answers: bench/NEXT_CASES.md D5."""

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from itertools import count
from pathlib import Path

import pytest

from pixel.config import settings
from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events
from pixel.core.context import Context
from pixel.pricing import calc, engine, pickup, store

ZONES = {
    z["zone"]: z for z in json.loads((Path(__file__).parent / "fixtures" / "pickup-memory.json").read_text())["zones"]
}
NOW = datetime.now(UTC)
SESSIONS = count(1)
CENTS = D("0.05")  # a line through prices rounded to cents


@pytest.fixture
def ctx(conn):
    ctx = Context(settings(), conn, None, None, None, None)
    names = {(zone, name) for zone, z in ZONES.items() for r in z["requests"] for name, _, _ in r["answers"]}
    store.save_carriers(ctx, [
        {"name": name, "kind": "trucker", "email": f"{name}@truck", "serves": " ".join(z for z, n in names if n == name)}
        for name in {n for _, n in names}
    ])  # fmt: skip
    return ctx


def asked(zone: str, request: dict) -> Rfq:
    return Rfq(session_id=next(SESSIONS), message_id=1, customer="c@x", origin=ZONES[zone]["airport"],
               destination="BBB", weight_kg=D(request["weight_kg"]), volume_cbm=D(request["volume_cbm"]),
               door_pickup=True, pickup_zone=zone, received_at=NOW, deadline_at=NOW)  # fmt: skip


def bid(rfq: Rfq, name: str, price: str, hours: int | None) -> RateOption:
    kg = calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    return RateOption(source=Source.BID, carrier=name, trucker=name, origin=rfq.origin, destination=rfq.destination,
                      chargeable_kg=kg, billed_kg=kg, freight=D(0), pickup=D(price), pickup_hours=hours,
                      total=D(price), currency="EUR", total_eur=D(price), transit_days=0, evidence_message_id=1)  # fmt: skip


def answered(ctx: Context, zone: str, request: dict) -> None:
    rfq = asked(zone, request)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=rfq.session_id, payload=rfq.model_dump(mode="json"))
    for answer in request["answers"]:
        events.emit(ctx, "option.found", "pricing", session_id=rfq.session_id,
                    payload={"option": bid(rfq, *answer).model_dump(mode="json")})  # fmt: skip


def chosen(rfq: Rfq, pickups: list[RateOption]) -> RateOption:
    air = bid(rfq, "Air", "0", None).model_copy(update={"trucker": None, "source": Source.SHEET})
    return engine.combine(rfq, [air, *pickups])[0]


def test_silent_truckers_get_the_price_and_the_trucker_their_earlier_answers_give(ctx):
    # Zone 61 as asked live, across three dataset versions and gross against volume weight: from the third request
    # on, the memory alone gives the trucker the balanced rule took from the real answers, at that answer's price.
    requests = ZONES["61"]["requests"]
    for known, request in enumerate(requests):
        rfq = asked("61", request)
        if known >= 2:
            real = chosen(rfq, [bid(rfq, *answer) for answer in request["answers"]])
            estimate = chosen(rfq, pickup.estimates(ctx, rfq, []))
            assert (estimate.trucker, estimate.pickup_hours) == (real.trucker, real.pickup_hours)
            assert abs(estimate.pickup - real.pickup) <= CENTS
        answered(ctx, "61", request)


def test_an_answer_at_another_price_level_does_not_move_a_truckers_line(ctx):
    # Zone 73: four requests at the usual prices, one while every price stood about 30 % higher.
    requests = ZONES["73"]["requests"]
    for request in requests:
        answered(ctx, "73", request)
    for request in requests:
        rfq = asked("73", request)
        for estimate in pickup.estimates(ctx, rfq, []):
            real = next(D(price) for name, price, _ in request["answers"] if name == estimate.trucker)
            assert (abs(estimate.pickup - real) <= CENTS) == ("level" not in request)


def test_a_trucker_with_one_answer_in_the_zone_follows_the_shape_of_the_known_lines(ctx):
    # Zone 73 asked once, at 352 kg; the next request weighs 1615 kg. The price as it was is half the real one;
    # moved along the lines known from zone 61 it is at most 11 % off.
    for request in ZONES["61"]["requests"]:
        answered(ctx, "61", request)
    first, second = ZONES["73"]["requests"][:2]
    answered(ctx, "73", first)
    rfq = asked("73", second)
    for estimate in pickup.estimates(ctx, rfq, []):
        real = next(D(price) for name, price, _ in second["answers"] if name == estimate.trucker)
        assert abs(estimate.pickup - real) <= real * D("0.15")


def test_an_estimate_stands_only_where_no_trucker_answered_and_says_so_once(ctx):
    for request in ZONES["61"]["requests"]:
        answered(ctx, "61", request)
    rfq = asked("61", ZONES["61"]["requests"][0])
    assert pickup.estimates(ctx, rfq, [bid(rfq, "Westfalenroute", "266.03", 23)]) == []
    assert len(pickup.estimates(ctx, rfq, [])) == len(pickup.estimates(ctx, rfq, [])) == 5
    said = ctx.db.execute("SELECT session_id, payload FROM events WHERE kind = 'pickup.estimated'").fetchall()
    assert [(s, json.loads(p)["estimated"], len(json.loads(p)["truckers"])) for s, p in said] == [
        (rfq.session_id, True, 5)
    ]
    # Not an `option.found`: the next request's memory and `feedback.pickup_rule` hold real answers only.
    assert not ctx.db.execute(
        "SELECT 1 FROM events WHERE kind = 'option.found' AND session_id = ?", (rfq.session_id,)
    ).fetchall()
