"""Subscribe the mailbox's pushes to our endpoint, signed with PIXEL_WEBHOOK_SECRET from .env, and test it.

uv run python -m tools.webhook https://<public host>/hook   # subscribe, then one signed test call
uv run python -m tools.webhook                              # the subscription and the last 20 pushes
"""

import asyncio
import json
import sys

from pixel.api.client import ApiError, make_client
from pixel.api.mailbox import Mailbox
from pixel.config import settings


async def _run(url: str | None) -> str:
    config = settings()
    async with make_client(config) as client:
        mailbox = Mailbox(client)
        try:
            if url is None:
                return json.dumps(await mailbox.webhook(), indent=2)
            if len(config.webhook_secret) < 16:
                return "failed: PIXEL_WEBHOOK_SECRET needs 16 or more characters"
            await mailbox.subscribe(url, config.webhook_secret)
            return json.dumps(await mailbox.webhook_test(), indent=2)
        except ApiError as exc:  # only the status: a validation error repeats what we sent
            return f"failed: {exc.status}"


def main() -> None:
    print(asyncio.run(_run(sys.argv[1] if len(sys.argv) > 1 else None)))


if __name__ == "__main__":
    main()
