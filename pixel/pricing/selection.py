"""Which option we quote. Pure functions over RateOption.

Ties: cheaper (in EUR) first, then faster, then the carrier name, so the same options always give the same quote.
"""

from collections.abc import Callable
from decimal import Decimal

from pixel.contracts import Preference, RateOption, Rfq, Source

HALF = Decimal("0.5")


def _cheapest(o: RateOption) -> tuple:
    return (o.total_eur, o.transit_days, o.carrier)


def _fastest(o: RateOption) -> tuple:
    return (o.transit_days, o.total_eur, o.carrier)


def _share(best: Decimal, value: Decimal) -> Decimal:
    """How close a value comes to the best one: 1 for the best, 0.5 for twice as much."""
    return Decimal(1) if value == best else (best / value if value else Decimal(0))


def balanced(bids: list[RateOption], speed: Callable[[RateOption], int] = lambda b: b.transit_days) -> RateOption:
    """Among carrier bids: price and speed weigh 50/50 (R15). Each bid scores its share of the best price and its
    share of the best speed (best / own), the highest equal-weight sum wins. `speed` is transit days for an airline,
    pickup hours for a trucker. Why this formula and not min-max scaling: docs/calc.md."""
    cheapest = min(b.total_eur for b in bids)
    quickest = min(Decimal(speed(b)) for b in bids)

    def score(b: RateOption) -> Decimal:
        return HALF * _share(cheapest, b.total_eur) + HALF * _share(quickest, Decimal(speed(b)))

    return min(bids, key=lambda b: (-score(b), *_cheapest(b)))


def select(rfq: Rfq, options: list[RateOption]) -> RateOption:
    """The one option to quote: cheapest or fastest as the customer asked, compared in EUR, all-in.
    Q6 assumption: carrier bids first pick their balanced winner, which then competes with sheets and portal."""
    if not options:
        raise ValueError("no option to select from")
    bids = [o for o in options if o.source == Source.BID]
    candidates = [o for o in options if o.source != Source.BID] + ([balanced(bids)] if bids else [])
    key = _fastest if rfq.preference == Preference.FASTEST else _cheapest
    return min(candidates, key=key)
