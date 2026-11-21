"""All arithmetic. Decimal only, ROUND_HALF_UP to two places at the end, never in a model."""

from decimal import ROUND_HALF_UP, Decimal

from pixel.contracts import LaneTariff, RateOption, Rfq, Source

CBM_KG = Decimal(167)
CENT = Decimal("0.01")

# R13: a higher weight break is billed when that makes the all-in total cheaper, on sheets and on the portal.
BILL_HIGHER_BREAK = True


def money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def chargeable_kg(weight_kg: Decimal, volume_cbm: Decimal) -> Decimal:
    return max(weight_kg, volume_cbm * CBM_KG)


def rate_for(breaks: tuple[tuple[Decimal, Decimal], ...], kg: Decimal) -> Decimal:
    """The rate of the bracket `kg` falls in: the last break at or below it."""
    rate = breaks[0][1]
    for start, value in breaks:
        if start <= kg:
            rate = value
    return rate


def _priced(tariff: LaneTariff, billed: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    freight = max(rate_for(tariff.breaks, billed) * billed, tariff.minimum)
    return freight, tariff.fuel_per_kg * billed, tariff.security_per_kg * billed


def price_tariff(tariff: LaneTariff, rfq: Rfq) -> RateOption:
    """Freight = rate of the bracket x billed weight, at least the minimum (before surcharges); fuel and
    security per kg of the billed weight. Takes the cheapest of the weight's own bracket and every higher break."""
    chargeable = chargeable_kg(rfq.weight_kg, rfq.volume_cbm)
    candidates = [chargeable]
    if BILL_HIGHER_BREAK:
        candidates += [start for start, _ in tariff.breaks if start > chargeable]
    billed = min(candidates, key=lambda kg: (sum(_priced(tariff, kg)), kg))
    freight, fuel, security = _priced(tariff, billed)
    total = money(freight + fuel + security)
    return RateOption(
        source=Source.SHEET,
        carrier=tariff.carrier,
        origin=tariff.origin,
        destination=tariff.destination,
        chargeable_kg=chargeable,
        billed_kg=billed,
        freight=money(freight),
        fuel=money(fuel),
        security=money(security),
        total=total,
        currency=tariff.currency,
        total_eur=to_eur(total, tariff.currency, None),
        transit_days=tariff.transit_days,
        dataset_version=tariff.version,
    )


def with_pickup(option: RateOption, pickup: Decimal, trucker: str) -> RateOption:
    """`pickup` is in EUR (trucker prices); it is converted into the option's currency when that is not EUR."""
    pickup_in_currency = pickup if option.currency == "EUR" else money(pickup / option.fx_eur)
    total = money(option.total + pickup_in_currency)
    return option.model_copy(
        update={
            "pickup": pickup_in_currency,
            "trucker": trucker,
            "total": total,
            "total_eur": to_eur(total, option.currency, option.fx_eur),
        }
    )


def to_eur(amount: Decimal, currency: str, fx_eur: Decimal | None) -> Decimal:
    if currency == "EUR":
        return money(amount)
    if fx_eur is None:
        raise ValueError(f"no exchange rate for {currency}")
    return money(amount * fx_eur)
