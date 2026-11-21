from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """One rate read per airline on the lane, most promising first, within the minute's read budget.
    Airlines the portal sends elsewhere (404, 503) are reported so `bids` can mail them."""
    raise NotImplementedError


async def refresh(ctx: Context, option: RateOption) -> RateOption:
    """A fresh rate_id for the same airline and lane, for a booking after 410."""
    raise NotImplementedError
