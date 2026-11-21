"""The only way mail leaves. Enforces first-quote-only, the recipient list, the mail limit, dry run and takeover."""

from pixel.contracts import Message, Quote
from pixel.core.context import Context


class NotSent(Exception):
    """Refused before the API was called: `reason` is dry_run, human, duplicate_quote or recipient."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def send(
    ctx: Context,
    *,
    to: str,
    subject: str,
    body: str,
    session_id: int | None,
    in_reply_to: int | None = None,
    purpose: str,
) -> Message:
    """Sends, stores the mail, emits `mail.out` (or `mail.held` and raises NotSent)."""
    raise NotImplementedError


async def send_quote(ctx: Context, quote: Quote) -> Quote:
    """Replies to the customer's RFQ mail. Atomic per session: a second quote for the same session raises NotSent."""
    raise NotImplementedError
