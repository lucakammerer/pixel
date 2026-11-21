from pixel.contracts import RateOption, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import bids, store


def zone_of(postcode: str) -> str:
    """Q13 assumption: the pickup zone is the first two digits of the German postcode."""
    return postcode.strip()[:2]


async def options(ctx: Context, rfq: Rfq, deadline: Deadline) -> list[RateOption]:
    """Door-to-port only: the truckers serving the pickup zone, asked by mail; one option per answer."""
    if not (rfq.door_pickup and rfq.pickup_zone):
        return []
    truckers = store.carriers(ctx, "trucker", rfq.pickup_zone)
    if not truckers:
        return []
    mailed = await bids.request(ctx, rfq, truckers, pickup=True)
    return await bids.collect(ctx, rfq.session_id, deadline, mailed, pickup=True)
