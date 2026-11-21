"""Store OPENAI_API_KEY from .env at the gateway (POST /v1/gateway/openai-key). Prints only whether it worked.

uv run python -m tools.set_openai_key
"""

import asyncio
import os

from pixel.api.client import ApiError, make_client
from pixel.api.gateway import Gateway
from pixel.config import settings


async def _store() -> str:
    config = settings()
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return "failed: OPENAI_API_KEY is empty"
    async with make_client(config) as client:
        try:
            await Gateway(client).store_key(key)
        except ApiError as exc:
            return f"failed: {exc.status}"
    return "stored"


def main() -> None:
    print(asyncio.run(_store()))


if __name__ == "__main__":
    main()
