"""Which option we quote. Pure functions over RateOption.

Ties: cheaper (in EUR) first, then faster, then the carrier name, so the same options always give the same quote.
"""

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

from pixel.contracts import Preference, RateOption, Rfq, Source
from pixel.pricing import calc

HALF = Decimal("0.5")
TRUST = Decimal("0.25")  # docs/calc.md, when a bid is not trusted


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


def implausible(option: RateOption, options: list[RateOption]) -> bool:
    """A carrier's bid below TRUST times the cheapest other carrier of its kind (truckers; airline options of the
    lane) was read wrong, whatever its layout (round 67: a waiting charge per hour taken for the pickup price).
    Only the cheapest bid of a kind can fail, so a trusted one of its kind always remains."""
    if option.source != Source.BID:
        return False
    own = option.trucker or option.carrier
    peers = [o.total_eur for o in options if (o.trucker is None) == (option.trucker is None)
             and (o.trucker or o.carrier) != own and o.total_eur > 0]  # fmt: skip
    return bool(peers) and option.total_eur < TRUST * min(peers)


def allowed(rfq: Rfq, options: list[RateOption]) -> list[RateOption]:
    """The options whose airline the customer did not rule out, by name or code (round 42: excluded_airline)."""
    out = {c.casefold() for c in rfq.excluded_carriers}
    return [o for o in options if o.carrier.casefold() not in out and (o.carrier_code or "").casefold() not in out]


def requested_carrier(rfq: Rfq, name: str, code: str | None = None) -> bool:
    """Whether the known name or code satisfies the request's airline restriction."""
    names = {carrier.casefold() for carrier in rfq.required_carriers}
    return not names or bool(names & {name.casefold(), (code or "").casefold()})


def requested_options(rfq: Rfq, options: list[RateOption]) -> list[RateOption]:
    return [option for option in allowed(rfq, options) if requested_carrier(rfq, option.carrier, option.carrier_code)]


def required(
    options: list[RateOption], only: Iterable[str] = (), arrive_by: date | None = None, ready: date | None = None
) -> list[RateOption]:
    """The options that meet what the customer requires beyond the price (`agents/rfq/novelty.py`): only the
    airlines they name, arriving by their date. When none does, all stay: an answer still beats none (R1)."""
    names = {n.casefold() for n in only}

    def named(o: RateOption) -> bool:
        carrier = o.carrier.casefold()
        return any(n in carrier or carrier in n or n == (o.carrier_code or "").casefold() for n in names)

    kept = [o for o in options if not names or named(o)]
    if arrive_by and ready:
        kept = [o for o in kept if calc.arrival(o, ready) <= arrive_by]
    return kept or options


def select(
    rfq: Rfq,
    options: list[RateOption],
    *,
    only: Iterable[str] = (),
    arrive_by: date | None = None,
    ready: date | None = None,
) -> RateOption:
    """The one option to quote: cheapest or fastest as the customer asked, compared in EUR, all-in. Airline bids
    compete like sheets and portal rates (round 28); only the trucker is chosen balanced (`engine.combine`)."""
    if not options:
        raise ValueError("no option to select from")
    options = allowed(rfq, options) or options  # with every airline ruled out, an answer still beats none (R1)
    if rfq.required_carriers:
        options = requested_options(rfq, options) or options
        only = ()
    options = required(options, only, arrive_by, ready)
    return min(options, key=_fastest if rfq.preference == Preference.FASTEST else _cheapest)
