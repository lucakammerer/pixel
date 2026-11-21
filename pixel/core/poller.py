"""Polls the mailbox, stores each mail once, hands it to the orchestrator."""

from collections.abc import Awaitable, Callable

from pixel.contracts import Message
from pixel.core.context import Context


async def run(ctx: Context, on_message: Callable[[Message], Awaitable[None]]) -> None:
    """Loops forever: poll with the stored `since_id`, store, emit `mail.in`, dispatch without blocking the loop.
    `since_id` is saved only after the mail is stored, so a restart repeats nothing and loses nothing."""
    raise NotImplementedError


def session_of(ctx: Context, message: Message) -> int:
    """The customer thread a mail belongs to: via `references`, or via the mail of ours it answers
    (a carrier bid answers a bid request we sent for a session)."""
    raise NotImplementedError
