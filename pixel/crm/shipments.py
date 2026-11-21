from pixel.contracts import Quote, Shipment
from pixel.core.context import Context


def create(ctx: Context, quote: Quote) -> Shipment:
    """A new shipment with a fresh id; that id is the booking's `team_reference`."""
    raise NotImplementedError


def save(ctx: Context, shipment: Shipment) -> None:
    raise NotImplementedError


def find(
    ctx: Context,
    *,
    reference: str | None = None,
    customer: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
) -> list[Shipment]:
    """What the tracking agent searches with: any id we or the airline gave, else customer and lane."""
    raise NotImplementedError
