"""POST /v1/gateway/*. Only `pixel/llm.py` and `tools/set_openai_key.py` import this."""

from typing import Any

import httpx

from pixel.api.client import ApiError


def _checked(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 300:
        raise ApiError(response.status_code, "gateway request failed")
    return response.json()


class Gateway:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def models(self) -> list[str]:
        body = _checked(await self.client.get("/v1/gateway/models"))
        return [m["id"] for m in body.get("data", [])]

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """OpenAI Chat Completions body in, response with `usage` out."""
        return _checked(await self.client.post("/v1/gateway/chat/completions", json=payload, timeout=60))

    async def has_key(self) -> bool:
        body = _checked(await self.client.get("/v1/gateway/openai-key"))
        return any(v is True for v in body.values())

    async def store_key(self, openai_key: str) -> None:
        _checked(await self.client.post("/v1/gateway/openai-key", json={"api_key": openai_key}))
