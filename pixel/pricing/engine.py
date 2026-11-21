from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Every priced option for the request: sheets (instant), portal and bids in parallel, pickup added for
    door-to-port. Emits `option.found` per option as it arrives, so the fallback always has the best so far."""
    raise NotImplementedError
