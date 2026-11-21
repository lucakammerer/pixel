from pixel.contracts import Message, Quote
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


async def handle(ctx: Context, message: Message, deadline: Deadline) -> Quote | None:
    """extract -> pricing.engine.options (emits `option.found` as they arrive) -> selection.select ->
    compose.render -> outbox.send_quote. Sends at `deadline.target_at` at the latest unless waiting can
    still change the answer and `hard_at` is not reached."""
    raise NotImplementedError


async def fallback(ctx: Context, session_id: int) -> Quote | None:
    """The best quote from the options stored so far, built by code alone. None when there is no price."""
    raise NotImplementedError
