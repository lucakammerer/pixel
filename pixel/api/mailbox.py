"""GET/POST /v1/mailbox/messages. Maps the API's mail shape to `contracts.Message`."""

import httpx

from pixel.contracts import Message


class Mailbox:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def poll(self, since_id: int, limit: int = 200) -> tuple[list[Message], int]:
        """Mail to us with id > since_id, oldest first, and `next_since_id`."""
        raise NotImplementedError

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: int | None = None,
        attachments: tuple[tuple[str, str, bytes], ...] = (),
    ) -> Message:
        """Raw send. Everything except `core/outbox.py` goes through the outbox instead."""
        raise NotImplementedError

    async def get(self, message_id: int) -> Message:
        raise NotImplementedError

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        raise NotImplementedError
