from pixel.contracts import Message, RateOption, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


async def request(ctx: Context, rfq: Rfq, carriers: list[tuple[str, str]]) -> list[int]:
    """One bid request per carrier (airline, trucker or the rate desk). Returns the sent mail ids; the poller
    maps the answers back to the session through them."""
    raise NotImplementedError


def parse(message: Message, rfq: Rfq) -> RateOption | None:
    """Price, currency and transit time from a carrier's answer, by rules. None when the rules cannot read it."""
    raise NotImplementedError


async def record(ctx: Context, message: Message) -> None:
    """Called by the orchestrator for every bid or desk answer: parse (model only if `parse` gives None),
    emit `bid.received` and `option.found`."""
    raise NotImplementedError


async def collect(ctx: Context, session_id: int, deadline: Deadline, expected: int) -> list[RateOption]:
    """Waits until all expected bids are in or the deadline's target, whichever comes first."""
    raise NotImplementedError
