"""Physical domain invariants for air freight forwarding.

Unlike keyword matching which breaks whenever phrasing shifts, invariants verify
fundamental physical and arithmetic properties of air freight:
- Transit bounds: realistic flight durations (1-30 days), preventing confusion with offer validity.
- Mass & Volume: chargeable weight must satisfy max(actual, volume * 167); piece-weight multipliers.
- Arithmetic balance: Total = freight + fuel + security + pickup.
- Unit conversions: deterministic normalization for lbs, tonnes, inches, and cubic meters.
"""

from decimal import Decimal, InvalidOperation

from pixel.contracts import RateOption, Rfq

MAX_AIR_TRANSIT_DAYS = 30
MIN_AIR_TRANSIT_DAYS = 1
MAX_REASONABLE_GROSS_KG = Decimal(100_000)
MIN_REASONABLE_GROSS_KG = Decimal("0.1")
CBM_FACTOR = Decimal(167)
LBS_TO_KG = Decimal("0.45359237")
TONNES_TO_KG = Decimal(1000)
INCH_TO_CM = Decimal("2.54")


class InvariantViolation(ValueError):
    """Raised when extracted or computed data violates a physical domain invariant."""


def is_valid_transit_duration(days: int | None) -> bool:
    """Air freight transit must be a positive duration between 1 and 30 days."""
    if days is None:
        return False
    return MIN_AIR_TRANSIT_DAYS <= days <= MAX_AIR_TRANSIT_DAYS


def calculate_chargeable_weight(actual_kg: Decimal, volume_cbm: Decimal = Decimal(0)) -> Decimal:
    """Chargeable weight invariant: max(actual gross kg, volume cbm * 167 kg)."""
    return max(actual_kg, volume_cbm * CBM_FACTOR)


def convert_weight_to_kg(value: Decimal | str | float, unit: str) -> Decimal:
    """Deterministically normalizes imperial and metric weight units to kilograms."""
    try:
        val = Decimal(str(value))
    except (InvalidOperation, TypeError) as e:
        raise InvariantViolation(f"Invalid weight numeric value: {value}") from e

    u = unit.strip().lower()
    if u in ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"):
        return val
    if u in ("lb", "lbs", "pound", "pounds"):
        return val * LBS_TO_KG
    if u in ("t", "ton", "tons", "tonne", "tonnes", "metric ton", "metric tonnes"):
        return val * TONNES_TO_KG
    raise InvariantViolation(f"Unsupported weight unit: {unit}")


def calculate_volume_cbm(
    length: Decimal | str,
    width: Decimal | str,
    height: Decimal | str,
    unit: str = "cm",
    pieces: int = 1,
) -> Decimal:
    """Calculates cubic meters from dimensions and piece count with strict unit normalization."""
    try:
        l_dec, w_dec, h_dec = Decimal(str(length)), Decimal(str(width)), Decimal(str(height))
    except (InvalidOperation, TypeError) as e:
        raise InvariantViolation(f"Invalid dimension values: {length}x{width}x{height}") from e

    u = unit.strip().lower()
    if u in ("cm", "centimeters"):
        divisor = Decimal(1_000_000)
    elif u in ("mm", "millimeters"):
        divisor = Decimal(1_000_000_000)
    elif u in ("m", "meters"):
        divisor = Decimal(1)
    elif u in ("in", "inch", "inches"):
        # 1 cubic inch = 1.6387064e-5 m³
        l_dec *= INCH_TO_CM
        w_dec *= INCH_TO_CM
        h_dec *= INCH_TO_CM
        divisor = Decimal(1_000_000)
    else:
        raise InvariantViolation(f"Unsupported dimension unit: {unit}")

    single_cbm = (l_dec * w_dec * h_dec) / divisor
    return single_cbm * Decimal(max(1, pieces))


def check_rate_option_invariants(option: RateOption) -> list[str]:
    """Audits a calculated RateOption against arithmetic and domain invariants.

    Returns a list of findings (empty when valid).
    """
    findings: list[str] = []

    # 1. Physical transit duration
    if not is_valid_transit_duration(option.transit_days):
        findings.append(f"Transit days {option.transit_days} outside air freight bounds (1-30)")

    # 2. Chargeable weight bounds
    if option.chargeable_kg <= 0:
        findings.append(f"Chargeable weight must be positive, got {option.chargeable_kg}")
    if option.billed_kg < option.chargeable_kg:
        findings.append(f"Billed weight {option.billed_kg} cannot be less than chargeable {option.chargeable_kg}")

    # 3. Arithmetic balance: total == freight + fuel + security + pickup
    expected_sum = option.freight + option.fuel + option.security + option.pickup
    # Allow 1 cent rounding divergence
    if abs(option.total - expected_sum) > Decimal("0.02"):
        findings.append(f"Total arithmetic mismatch: total {option.total} != components sum {expected_sum}")

    # 4. Currency and FX sanity
    if option.currency != "EUR":
        if option.fx_eur is None or option.fx_eur <= 0:
            findings.append(f"Missing or invalid FX rate for non-EUR currency {option.currency}")
        elif option.total_eur <= 0:
            findings.append(f"total_eur {option.total_eur} must be positive")

    return findings


def audit_extracted_rfq(rfq: Rfq) -> list[str]:
    """Audits extracted customer RFQ facts against physical sanity bounds.

    Detects common extraction corruptions such as reading telephone numbers or postal codes as weights.
    """
    findings: list[str] = []

    # 1. Weight bounds
    if rfq.weight_kg <= 0:
        findings.append(f"Gross weight must be positive, got {rfq.weight_kg}")
    elif rfq.weight_kg > MAX_REASONABLE_GROSS_KG:
        findings.append(f"Gross weight {rfq.weight_kg} exceeds maximum commercial payload (100t)")
    elif rfq.weight_kg < MIN_REASONABLE_GROSS_KG:
        findings.append(f"Gross weight {rfq.weight_kg} suspiciously small (< 100g)")

    # 2. Volume bounds
    if rfq.volume_cbm < 0:
        findings.append(f"Volume cannot be negative, got {rfq.volume_cbm}")

    # 3. Origin != Destination
    if rfq.origin and rfq.destination and rfq.origin.upper() == rfq.destination.upper():
        findings.append(f"Origin and destination cannot be identical ({rfq.origin})")

    # 4. Pieces sanity
    if rfq.pieces is not None and rfq.pieces <= 0:
        findings.append(f"Piece count must be positive, got {rfq.pieces}")

    return findings
