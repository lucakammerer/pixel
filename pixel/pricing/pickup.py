from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


def zone_of(postcode: str) -> str:
    raise NotImplementedError


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Door-to-port only: the truckers serving the pickup zone, asked by mail; one option per answer."""
    raise NotImplementedError
