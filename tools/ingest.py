"""Ingest a folder of tariffs, the carrier list and the pickup zones into the tariff store.

uv run python -m tools.ingest data/start-kit
"""

import asyncio
import sys
from pathlib import Path

from pixel.agents.tariffs.agent import ingest_folder
from pixel.app import build_context


async def _run(folder: Path) -> None:
    ctx = build_context()
    try:
        await ingest_folder(ctx, folder)
        for row in ctx.db.execute("SELECT carrier, COUNT(*) AS lanes FROM tariffs GROUP BY carrier ORDER BY carrier"):
            print(f"{row['lanes']:>4} lanes  {row['carrier']}")
        for row in ctx.db.execute("SELECT payload FROM events WHERE kind = 'tariff.failed' ORDER BY id"):
            print("failed:", row["payload"])
    finally:
        await ctx.mailbox.client.aclose()
        ctx.db.close()


def main() -> None:
    asyncio.run(_run(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
