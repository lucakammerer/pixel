import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from pixel.contracts import Preference, RateOption, Rfq, Source
from pixel.pricing.selection import balanced, select

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
    # share of the best (price, days): X (1, 0.25), Y (0.5, 1), Z (0.8, 0.67): Y has the highest sum
    bids = [opt("X", "400", 8, Source.BID), opt("Y", "800", 2, Source.BID), opt("Z", "500", 3, Source.BID)]
    assert balanced(bids).carrier == "Y"


def test_balanced_picks_the_trucker_the_scorer_picked():
    for request in json.loads((Path(__file__).parent / "fixtures" / "pickup-bids.json").read_text())["requests"]:
        bids = [opt(name, eur, 0, Source.BID).model_copy(update={"trucker": name, "pickup_hours": hours})
                for name, eur, hours in request["bids"]]  # fmt: skip
        assert balanced(bids, speed=lambda b: b.pickup_hours).trucker == request["scorer"]


def test_only_the_balanced_bid_competes_with_other_sources():
    # balanced: X 0 + 0.5, Y 0.1 + 0, W 0.5 + 0.21 -> Y (320); the sheet at 310 beats Y, X at 300 never competes
    bids = [opt("X", "300", 9, Source.BID), opt("Y", "320", 2, Source.BID), opt("W", "400", 5, Source.BID)]
    assert select(rfq(Preference.CHEAPEST), bids + [opt("S", "310", 4)]).carrier == "S"
    assert select(rfq(Preference.CHEAPEST), bids + [opt("S", "350", 4)]).carrier == "Y"
