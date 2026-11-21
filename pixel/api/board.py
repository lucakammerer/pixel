"""GET /v1/board and /v1/board/me."""

from typing import Any

import httpx

from pixel.api.client import request


class Board:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def public(self) -> dict[str, Any]:
        return (await request(self.client, "GET", "/v1/board")).json()

    async def me(self) -> dict[str, Any]:
        """Per request: round, sent and answered time, verdict, reason code, points per kind, booked."""
        return (await request(self.client, "GET", "/v1/board/me")).json()
