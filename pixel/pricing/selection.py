"""Which option we quote. Pure functions over RateOption.

Ties: cheaper (in EUR) first, then faster, then the carrier name, so the same options always give the same quote.
"""

from decimal import Decimal

from pixel.contracts import Preference, RateOption, Rfq, Source

HALF = Decimal("0.5")


def _cheapest(o: RateOption) -> tuple:
    return (o.total_eur, o.transit_days, o.carrier)


def _fastest(o: RateOption) -> tuple:
    return (o.transit_days, o.total_eur, o.carrier)


def _scaled(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return Decimal(0) if high == low else (value - low) / (high - low)


def balanced(bids: list[RateOption]) -> RateOption:
    """Among carrier bids: price and speed weigh 50/50 (R15). Q6 assumption: price (EUR) and transit days are
    min-max scaled over the bids, the lowest equal-weight sum wins."""
    prices = [b.total_eur for b in bids]
    days = [Decimal(b.transit_days) for b in bids]

    def score(b: RateOption) -> Decimal:
        price = _scaled(b.total_eur, min(prices), max(prices))
        speed = _scaled(Decimal(b.transit_days), min(days), max(days))
        return HALF * price + HALF * speed

    return min(bids, key=lambda b: (score(b), *_cheapest(b)))


def select(rfq: Rfq, options: list[RateOption]) -> RateOption:
    """The one option to quote: cheapest or fastest as the customer asked, compared in EUR, all-in.
    Q6 assumption: carrier bids first pick their balanced winner, which then competes with sheets and portal."""
    if not options:
        raise ValueError("no option to select from")
    bids = [o for o in options if o.source == Source.BID]
    candidates = [o for o in options if o.source != Source.BID] + ([balanced(bids)] if bids else [])
    key = _fastest if rfq.preference == Preference.FASTEST else _cheapest
    return min(candidates, key=key)
