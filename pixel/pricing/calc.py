"""All arithmetic. Decimal only, ROUND_HALF_UP to two places at the end, never in a model."""

from decimal import Decimal

from pixel.contracts import LaneTariff, RateOption, Rfq

CBM_KG = Decimal(167)


def chargeable_kg(weight_kg: Decimal, volume_cbm: Decimal) -> Decimal:
    raise NotImplementedError


def price_tariff(tariff: LaneTariff, rfq: Rfq) -> RateOption:
    """Freight = rate of the bracket x billed weight, at least the minimum (before surcharges); fuel and
    security per kg of the billed weight. Takes the cheaper of the weight's own bracket and the next break."""
    raise NotImplementedError


def with_pickup(option: RateOption, pickup: Decimal, trucker: str) -> RateOption:
    raise NotImplementedError


def to_eur(amount: Decimal, currency: str, fx_eur: Decimal | None) -> Decimal:
    raise NotImplementedError
