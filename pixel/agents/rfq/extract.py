from pixel.contracts import Message, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline


def by_rules(message: Message, deadline: Deadline) -> Rfq | None:
    """Regex and lookup tables. None unless origin, destination and weight are certain."""
    raise NotImplementedError


async def extract(ctx: Context, message: Message, deadline: Deadline) -> Rfq:
    """by_rules, else one `llm.ask_json` call. Emits `rfq.extracted` with `extracted_by`."""
    raise NotImplementedError
