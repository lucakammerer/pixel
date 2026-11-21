"""Tests for physical domain invariants and adversarial fuzzing resilience."""

from decimal import Decimal

from pixel.contracts import RateOption, Source
from pixel.pricing.invariants import (
    calculate_chargeable_weight,
    calculate_volume_cbm,
    check_rate_option_invariants,
    convert_weight_to_kg,
    is_valid_transit_duration,
)
from tools.fuzzer import run_fuzzer_suite


def test_transit_duration_bounds():
    assert is_valid_transit_duration(1)
    assert is_valid_transit_duration(14)
    assert is_valid_transit_duration(30)
    assert not is_valid_transit_duration(0)
    assert not is_valid_transit_duration(-3)
    assert not is_valid_transit_duration(35)
    assert not is_valid_transit_duration(None)


def test_unit_conversions_deterministic():
    assert convert_weight_to_kg("100", "kg") == Decimal(100)
    assert abs(convert_weight_to_kg("550", "lbs") - Decimal("249.48")) < Decimal("0.1")
    assert convert_weight_to_kg("2.5", "tonnes") == Decimal(2500)


def test_dimensional_volume_conversions():
    # 120 x 80 x 160 cm = 1.536 m³
    assert calculate_volume_cbm("120", "80", "160", "cm") == Decimal("1.536")
    # In millimeters: 1200 x 800 x 1600 mm = 1.536 m³
    assert calculate_volume_cbm("1200", "800", "1600", "mm") == Decimal("1.536")
    # In inches: 48 x 40 x 60 in = ~1.888 m³
    vol_in = calculate_volume_cbm("48", "40", "60", "in", pieces=2)
    assert abs(vol_in - Decimal("3.775")) < Decimal("0.05")


def test_chargeable_weight_invariant():
    # Actual weight exceeds volume weight
    assert calculate_chargeable_weight(Decimal(500), Decimal("1.0")) == Decimal(500)
    # Volume weight exceeds actual weight (1.0 m³ * 167 = 167 kg)
    assert calculate_chargeable_weight(Decimal(100), Decimal("1.0")) == Decimal(167)


def test_rate_option_arithmetic_auditor():
    valid_option = RateOption(
        source=Source.SHEET,
        carrier="Lufthansa",
        origin="FRA",
        destination="JFK",
        chargeable_kg=Decimal(100),
        billed_kg=Decimal(100),
        freight=Decimal(200),
        fuel=Decimal(50),
        security=Decimal(20),
        pickup=Decimal(30),
        total=Decimal(300),
        currency="EUR",
        total_eur=Decimal(300),
        transit_days=2,
    )
    assert check_rate_option_invariants(valid_option) == []

    # Corrupt total arithmetic
    corrupted_total = valid_option.model_copy(update={"total": Decimal(999)})
    assert len(check_rate_option_invariants(corrupted_total)) > 0

    # Corrupt transit duration
    corrupted_transit = valid_option.model_copy(update={"transit_days": 180})
    assert len(check_rate_option_invariants(corrupted_transit)) > 0


def test_adversarial_fuzzer_resilience():
    """Executes the full adversarial mutation suite; all mutations must be safely handled."""
    passed, total, failures = run_fuzzer_suite()
    assert not failures, f"Fuzzer detected invariant failures: {failures}"
    assert passed == total == 18
