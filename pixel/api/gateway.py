"""POST /v1/gateway/*. Only `pixel/llm.py` and `tools/set_openai_key.py` import this."""

from typing import Any

import httpx


class Gateway:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def models(self) -> list[str]:
        raise NotImplementedError

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """OpenAI Chat Completions body in, response with `usage` out."""
        raise NotImplementedError

    async def has_key(self) -> bool:
        raise NotImplementedError

    async def store_key(self, openai_key: str) -> None:
        raise NotImplementedError
