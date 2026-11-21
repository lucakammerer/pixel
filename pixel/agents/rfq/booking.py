from pixel.contracts import Booking, Message
from pixel.core.context import Context


async def handle_reply(ctx: Context, message: Message) -> None:
    """The customer answered our quote: acceptance -> book; decline -> quote declined; question -> answer."""
    raise NotImplementedError


async def book(ctx: Context, quote_id: str) -> Booking:
    """Creates the shipment (its CRM id is the `team_reference`), books by `rate_id`; on 410 fetches a fresh
    rate and books that; on 503 or without a rate_id books by email with the carrier. Tells the customer."""
    raise NotImplementedError


async def handle_confirmation(ctx: Context, message: Message) -> None:
    """ "Booking <reference> confirmed" from the airline -> booking confirmed, shipment updated, customer told."""
    raise NotImplementedError
