from pixel.contracts import Message
from pixel.core.context import Context


async def handle(ctx: Context, message: Message) -> None:
    """Finds the shipment the question means (booking reference, our reference, customer, lane) in the CRM and
    answers in the thread from the CRM alone. Says so plainly when we hold no such shipment."""
    raise NotImplementedError
