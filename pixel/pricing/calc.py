"""All arithmetic. Decimal only, ROUND_HALF_UP to two places at the end, never in a model."""

from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from pixel.contracts import LaneTariff, RateOption, Rfq, Source

CBM_KG = Decimal(167)
CENT = Decimal("0.01")
OFFICE = ZoneInfo("Europe/Berlin")  # a sheet's last day ends on our desk's clock

# R13: a higher weight break is billed when that makes the all-in total cheaper, on sheets and on the portal.
BILL_HIGHER_BREAK = True


def money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def chargeable_kg(weight_kg: Decimal, volume_cbm: Decimal) -> Decimal:
    return max(weight_kg, volume_cbm * CBM_KG)


def bid_charge(amount: Decimal, per_kg: bool, rfq: Rfq) -> Decimal:
    """A carrier's quoted flat charge or rate per chargeable kilogram, before final rounding."""
    return amount * chargeable_kg(rfq.weight_kg, rfq.volume_cbm) if per_kg else amount


def bid_total(charges: list[tuple[Decimal, bool]], rfq: Rfq) -> Decimal:
    return sum((bid_charge(amount, per_kg, rfq) for amount, per_kg in charges), Decimal(0))


def ready_day(rfq: Rfq) -> date:
    """A shipment already ready can first ship on the request date."""
    received = rfq.received_at.date()
    return max(rfq.ready_on, received) if rfq.ready_on else received


def arrival(option: RateOption, ready: date) -> date:
    """The ready date plus the transit, a day more when a trucker first takes the cargo to the airport."""
    return ready + timedelta(days=option.transit_days + (1 if option.trucker else 0))


def transit_days(value: int, hours: bool) -> int:
    """Whole days on the quote, rounded up so an hourly bid never promises an earlier arrival."""
    return (value + 23) // 24 if hours else value


HOURS_PER = {"minutes": Decimal(1) / 60, "hours": Decimal(1), "days": Decimal(24)}


def pickup_hours(value: Decimal, unit: str) -> int:
    """A trucker's pickup time in whole hours, as the bid compares it: "2 days" is 48, "90 minutes" 2, half up."""
    return int((value * HOURS_PER[unit]).to_integral_value(ROUND_HALF_UP))


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


def price_tariff(tariff: LaneTariff, rfq: Rfq, fx_eur: Decimal | None = None) -> RateOption:
    """Freight = rate of the bracket x billed weight, at least the minimum (before surcharges); fuel and
    security per kg of the billed weight. Takes the cheapest of the weight's own bracket and every higher break.
    A sheet in another currency needs `fx_eur`, EUR per unit of it."""
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
        fx_eur=None if tariff.currency == "EUR" else fx_eur,
        total_eur=to_eur(total, tariff.currency, fx_eur),
        transit_days=tariff.transit_days,
        valid_until=datetime.combine(tariff.valid_to, time(23, 59), OFFICE),
        dataset_version=tariff.version,
    )


def air_price(option: RateOption) -> Decimal:
    """Purchase price of the airline part, in the currency of the accepted quote."""
    return option.total - option.pickup


def per_kg(option: RateOption) -> Decimal:
    """The all-in rate per kg of chargeable weight."""
    return money(option.total / option.chargeable_kg)


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


def insurance_premium(value: Decimal, share_pct: Decimal, rate_pct: Decimal, minimum: Decimal = Decimal(0)) -> Decimal:
    """Goods value x insured share x premium rate, at least the minimum premium."""
    return money(max(value * share_pct / 100 * rate_pct / 100, minimum))


def with_insurance(option: RateOption, premium_eur: Decimal) -> RateOption:
    """The option's all-in with the insurance premium (in EUR) added once, in the option's currency."""
    premium = insurance_in_currency(option, premium_eur)
    total = money(option.total + premium)
    return option.model_copy(update={"total": total, "total_eur": to_eur(total, option.currency, option.fx_eur)})


def insurance_in_currency(option: RateOption, premium_eur: Decimal) -> Decimal:
    return premium_eur if option.currency == "EUR" or not premium_eur else money(premium_eur / option.fx_eur)


def to_eur(amount: Decimal, currency: str, fx_eur: Decimal | None) -> Decimal:
    if currency == "EUR":
        return money(amount)
    if fx_eur is None:
        raise ValueError(f"no exchange rate for {currency}")
    return money(amount * fx_eur)


def in_currency(option: RateOption, currency: str, fx_eur: Decimal | None = None) -> RateOption:
    """Convert a selected option while preserving its EUR valuation."""
    if currency == option.currency:
        return option
    source_fx = Decimal(1) if option.currency == "EUR" else option.fx_eur
    if source_fx is None:
        return option
    if currency == "EUR":
        target_fx = Decimal(1)
        total = option.total_eur
        fx_eur = None
    else:
        if fx_eur is None or not fx_eur.is_finite() or fx_eur <= 0:
            return option
        target_fx = fx_eur
        total = money(option.total_eur / target_fx)
    return option.model_copy(
        update={
            "freight": money(option.freight * source_fx / target_fx),
            "fuel": money(option.fuel * source_fx / target_fx),
            "security": money(option.security * source_fx / target_fx),
            "pickup": money(option.pickup * source_fx / target_fx),
            "currency": currency,
            "fx_eur": fx_eur,
            "total": total,
        }
    )
