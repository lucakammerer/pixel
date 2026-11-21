import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel.agents.rfq import check
from pixel.contracts import Preference, Quote, RateOption, Rfq, Source
from pixel.pricing import calc, engine
from pixel.pricing.selection import DEFAULT_RULE, RULES, balanced, implausible, select

NOW = datetime(2026, 11, 21, 16, tzinfo=UTC)


def opt(carrier: str, eur: str, days: int, source: Source = Source.SHEET) -> RateOption:
    return RateOption(
        source=source, carrier=carrier, origin="FRA", destination="JFK", chargeable_kg=D(100), billed_kg=D(100),
        freight=D(eur), total=D(eur), currency="EUR", total_eur=D(eur), transit_days=days,
    )  # fmt: skip


def rfq(preference: Preference) -> Rfq:
    return Rfq(
        session_id=1, message_id=1, customer="c@x", origin="FRA", destination="JFK", weight_kg=D(100),
        preference=preference, received_at=NOW, deadline_at=NOW,
    )  # fmt: skip


def test_cheapest_and_fastest():
    options = [opt("A", "500", 5), opt("B", "400", 6), opt("C", "700", 2)]
    assert select(rfq(Preference.CHEAPEST), options).carrier == "B"
    assert select(rfq(Preference.FASTEST), options).carrier == "C"


def test_ties_are_decided_by_speed_then_name():
    assert select(rfq(Preference.CHEAPEST), [opt("B", "400", 4), opt("A", "400", 4), opt("C", "400", 3)]).carrier == "C"
    assert select(rfq(Preference.CHEAPEST), [opt("B", "400", 4), opt("A", "400", 4)]).carrier == "A"


def test_balanced_weighs_price_and_speed_equally():
    # times the best (price, days): X (1, 4), Y (2, 1), Z (1.25, 1.5): Z has the lowest sum
    bids = [opt("X", "400", 8, Source.BID), opt("Y", "800", 2, Source.BID), opt("Z", "500", 3, Source.BID)]
    assert balanced(bids).carrier == "Z"


def test_only_the_default_rule_picks_the_scorers_trucker_in_every_scored_request():
    requests = json.loads((Path(__file__).parent / "fixtures" / "pickup-bids.json").read_text())["requests"]
    fits = {}
    for rule in RULES:
        fits[rule] = all(
            balanced([opt(name, eur, 0, Source.BID).model_copy(update={"trucker": name, "pickup_hours": hours})
                      for name, eur, hours in r["bids"]], lambda b: b.pickup_hours, rule).trucker == r["scorer"]
            for r in requests
        )  # fmt: skip
    assert fits == {rule: rule == DEFAULT_RULE for rule in RULES}


def test_balanced_picks_the_trucker_the_scorer_picked():
    for request in json.loads((Path(__file__).parent / "fixtures" / "pickup-bids.json").read_text())["requests"]:
        bids = [opt(name, eur, 0, Source.BID).model_copy(update={"trucker": name, "pickup_hours": hours})
                for name, eur, hours in request["bids"]]  # fmt: skip
        assert balanced(bids, speed=lambda b: b.pickup_hours).trucker == request["scorer"]


def test_airline_bids_compete_by_the_customers_wish():
    scored = json.loads((Path(__file__).parent / "fixtures" / "scored-airline-bids.json").read_text())
    options = [opt(o["carrier"], o["total"], o["days"], Source(o["source"])) for o in scored["options"]]
    assert select(rfq(Preference(scored["preference"])), options).carrier == scored["scorer_carrier"]


def test_an_airline_the_customer_ruled_out_is_never_quoted():
    scored = json.loads((Path(__file__).parent / "fixtures" / "scored-excluded-airline.json").read_text())
    options = [opt(o["carrier"], o["total"], o["days"], Source(o["source"])) for o in scored["options"]]
    by_name = rfq(Preference(scored["preference"])).model_copy(update={"excluded_carriers": (scored["excluded"],)})
    assert select(by_name, options).carrier != scored["excluded"]
    portal = [o.model_copy(update={"carrier_code": scored["code"]}) if o.carrier == scored["excluded"] else o
              for o in options]  # fmt: skip
    by_code = by_name.model_copy(update={"excluded_carriers": (scored["code"],)})
    assert select(by_code, portal).carrier != scored["excluded"]


PLAUSIBILITY = json.loads((Path(__file__).parent / "fixtures" / "bid-plausibility.json").read_text())


def test_no_bid_read_right_in_a_recorded_round_is_distrusted():
    for r in PLAUSIBILITY["truckers"]:
        bids = [opt(name, eur, 0, Source.BID).model_copy(update={"trucker": name}) for name, eur, _ in r["bids"]]
        assert not [b.trucker for b in bids if implausible(b, bids)], r["round"]
    for r in PLAUSIBILITY["airlines"]:
        options = [opt(carrier, eur, 1, Source(source)) for source, carrier, eur in r["options"]]
        assert not [o.carrier for o in options if implausible(o, options)], r["round"]


def test_a_misread_trucker_bid_is_not_quoted():
    case = PLAUSIBILITY["misread"]
    origin, destination = case["lane"]
    request = rfq(Preference(case["preference"])).model_copy(
        update={"origin": origin, "destination": destination, "weight_kg": D(case["weight_kg"]), "door_pickup": True}
    )
    lane = {"origin": origin, "destination": destination}
    airlines = [opt(carrier, eur, days, Source(source)).model_copy(update=lane)
                for source, carrier, eur, days in case["airlines"]]  # fmt: skip
    bids = [opt(name, eur, 0, Source.BID).model_copy(update={**lane, "freight": D(0), "pickup": D(eur), "trucker": name,
                                                             "pickup_hours": hours})
            for name, eur, hours in case["bids"]]  # fmt: skip
    options = airlines + bids
    quoted = select(request, engine.combine(request, options))
    assert (quoted.trucker, quoted.total) == (case["right"]["trucker"], D(case["right"]["total"]))
    misread = select(request, [calc.with_pickup(a, D("45.00"), case["misread"]) for a in airlines])
    assert misread.total == D(case["quoted"])

    def blocked(option: RateOption) -> list[str]:
        quote = Quote(id="q", session_id=1, rfq=request, option=option, total=option.total, currency="EUR",
                      subject="s", body="b", created_at=NOW)  # fmt: skip
        return [f.rule for f in check.run(quote, "", options) if f.level == check.BLOCK]

    assert "implausible_bid" in blocked(misread) and "implausible_bid" not in blocked(quoted)
