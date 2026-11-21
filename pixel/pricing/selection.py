"""Which option we quote. Pure functions over RateOption.

Ties: cheaper (in EUR) first, then faster, then the carrier name, so the same options always give the same quote.
"""

from collections.abc import Callable
from decimal import Decimal

from pixel.contracts import Preference, RateOption, Rfq

HALF = Decimal("0.5")


def _cheapest(o: RateOption) -> tuple:
    return (o.total_eur, o.transit_days, o.carrier)


def _fastest(o: RateOption) -> tuple:
    return (o.transit_days, o.total_eur, o.carrier)


def _share(best: Decimal, value: Decimal) -> Decimal:
    """How close a value comes to the best one: 1 for the best, 0.5 for twice as much."""
    return Decimal(1) if value == best else (best / value if value else Decimal(0))


def _scaled(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return Decimal(0) if high == low else (value - low) / (high - low)


# What "balanced" can mean, as a cost per value (lower is better). The first is used until scored requests say
# otherwise: `core/feedback.py` `pickup_rule` counts which rule picks the trucker the scorer picked.
RULES: dict[str, Callable[[Decimal, list[Decimal]], Decimal]] = {
    "times_the_best": lambda value, values: value / min(values) if min(values) else value,
    "share_of_best": lambda value, values: -_share(min(values), value),
    "share_of_worst": lambda value, values: value / max(values) if max(values) else Decimal(0),
    "min_max": lambda value, values: _scaled(value, min(values), max(values)),
}
DEFAULT_RULE = next(iter(RULES))


def balanced(
    bids: list[RateOption], speed: Callable[[RateOption], int] = lambda b: b.transit_days, rule: str = DEFAULT_RULE
) -> RateOption:
    """Among carrier bids: price and speed weigh 50/50 (R15), compared by one of `RULES`. `speed` is transit days
    for an airline, pickup hours for a trucker. Why times-the-best is the default: docs/calc.md."""
    cost = RULES[rule]
    prices = [b.total_eur for b in bids]
    speeds = [Decimal(speed(b)) for b in bids]

    def score(b: RateOption) -> Decimal:
        return HALF * cost(b.total_eur, prices) + HALF * cost(Decimal(speed(b)), speeds)

    return min(bids, key=lambda b: (score(b), *_cheapest(b)))


def allowed(rfq: Rfq, options: list[RateOption]) -> list[RateOption]:
    """The options whose airline the customer did not rule out, by name or code (round 42: excluded_airline)."""
    out = {c.casefold() for c in rfq.excluded_carriers}
    return [o for o in options if o.carrier.casefold() not in out and (o.carrier_code or "").casefold() not in out]


def select(rfq: Rfq, options: list[RateOption]) -> RateOption:
    """The one option to quote: cheapest or fastest as the customer asked, compared in EUR, all-in. Airline bids
    compete like sheets and portal rates (round 28); only the trucker is chosen balanced (`engine.combine`)."""
    if not options:
        raise ValueError("no option to select from")
    options = allowed(rfq, options) or options  # with every airline ruled out, an answer still beats none (R1)
    return min(options, key=_fastest if rfq.preference == Preference.FASTEST else _cheapest)
