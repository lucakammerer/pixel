"""GET /v1/board and /v1/board/me."""

from typing import Any

import httpx


class Board:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def public(self) -> dict[str, Any]:
        raise NotImplementedError

    async def me(self) -> dict[str, Any]:
        """Per request: round, sent and answered time, verdict, reason code, points per kind, booked."""
        raise NotImplementedError
