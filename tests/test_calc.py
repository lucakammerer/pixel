from datetime import UTC, date, datetime
from decimal import Decimal as D

from pixel.contracts import LaneTariff, Rfq
from pixel.pricing.calc import chargeable_kg, price_tariff, to_eur, with_pickup

TARIFF = LaneTariff(
    carrier="Test Air", origin="FRA", destination="JFK", currency="EUR", valid_from=date(2026, 11, 1),
    valid_to=date(2026, 11, 30), version="v1", transit_days=3, minimum=D("90.00"),
    breaks=((D(0), D("4.00")), (D(45), D("3.00")), (D(100), D("2.50")), (D(300), D("2.00")), (D(500), D("1.50"))),
    fuel_per_kg=D("0.80"), security_per_kg=D("0.20"), source="test",
)  # fmt: skip


def rfq(weight: str, volume: str = "0") -> Rfq:
    now = datetime(2026, 11, 21, 16, tzinfo=UTC)
    return Rfq(
        session_id=1, message_id=1, customer="c@x", origin="FRA", destination="JFK", weight_kg=D(weight),
        volume_cbm=D(volume), received_at=now, deadline_at=now,
    )  # fmt: skip


def test_volume_weight_above_actual():
    assert chargeable_kg(D(100), D("1.5")) == D("250.5")
    o = price_tariff(TARIFF, rfq("100", "1.5"))
    assert (o.chargeable_kg, o.billed_kg, o.freight) == (D("250.5"), D("250.5"), D("626.25"))


def test_minimum_applies_to_freight_before_surcharges():
    o = price_tariff(TARIFF, rfq("10"))
    assert (o.freight, o.fuel, o.security, o.total) == (D("90.00"), D("8.00"), D("2.00"), D("100.00"))


def test_weight_exactly_on_a_break_takes_that_break():
    o = price_tariff(TARIFF, rfq("100"))
    assert (o.billed_kg, o.freight, o.total) == (D(100), D("250.00"), D("350.00"))


def test_cheaper_higher_break_billed_with_surcharges_on_billed_weight():
    # 280 kg at 2.50 + 1.00 = 980.00; billed at 300 kg: 600 + 300 = 900.00
    o = price_tariff(TARIFF, rfq("280"))
    assert (o.billed_kg, o.freight, o.fuel, o.security, o.total) == (
        D(300),
        D("600.00"),
        D("240.00"),
        D("60.00"),
        D("900.00"),
    )


def test_higher_break_not_taken_when_surcharges_make_it_dearer():
    # 230 kg: 230 x 3.50 = 805.00; at 300 kg: 300 x 3.00 = 900.00
    assert price_tariff(TARIFF, rfq("230")).billed_kg == D(230)


def test_top_break():
    o = price_tariff(TARIFF, rfq("2500"))
    assert (o.billed_kg, o.freight, o.total) == (D(2500), D("3750.00"), D("6250.00"))


def test_rounding_half_up_once_on_the_total():
    o = price_tariff(TARIFF, rfq("100.005"))
    assert o.total == D("350.02")  # 350.0175 -> 350.02


def test_pickup_and_fx():
    usd = price_tariff(TARIFF, rfq("100")).model_copy(update={"currency": "USD", "fx_eur": D("0.90")})
    o = with_pickup(usd, D("45.00"), "Trucker")
    assert (o.pickup, o.total, o.total_eur, o.trucker) == (D("50.00"), D("400.00"), D("360.00"), "Trucker")
    assert to_eur(D(10), "EUR", None) == D("10.00")
