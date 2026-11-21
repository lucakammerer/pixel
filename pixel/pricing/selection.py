"""Which option we quote. Pure functions over RateOption."""

from pixel.contracts import RateOption, Rfq


def balanced(bids: list[RateOption]) -> RateOption:
    """Among carrier bids: price and speed weigh 50/50."""
    raise NotImplementedError


def select(rfq: Rfq, options: list[RateOption]) -> RateOption:
    """The one option to quote: cheapest or fastest as the customer asked, compared in EUR, all-in."""
    raise NotImplementedError
