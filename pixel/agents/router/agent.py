from pixel.contracts import MailKind, Message
from pixel.core.context import Context


async def classify(ctx: Context, message: Message) -> MailKind:
    """Rules first: sender (desk, tracking, carrier list), thread (reply to a bid request, to a quote),
    attachments (tariff). A model call only for a customer mail the rules cannot place."""
    raise NotImplementedError
