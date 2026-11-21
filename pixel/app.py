"""Process entry: `uv run uvicorn pixel.app:app`. One process runs the poller, the handlers and the UI."""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from pixel.api.board import Board
from pixel.api.client import make_client
from pixel.api.gateway import Gateway
from pixel.api.mailbox import Mailbox
from pixel.api.portal import Portal
from pixel.config import Settings, settings
from pixel.core import db, orchestrator, poller
from pixel.core.context import Context
from pixel.core.events import emit
from pixel.ui import dashboard, routes


def build_context(config: Settings | None = None) -> Context:
    """Settings, database (`db.init`), API clients -> Context."""
    config = config or settings()
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(config.db_path)
    db.init(conn)
    client = make_client(config)
    return Context(
        settings=config,
        db=conn,
        mailbox=Mailbox(client),
        portal=Portal(client),
        gateway=Gateway(client),
        board=Board(client),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """build_context, orchestrator.recover, then poller.run as a background task; cancels it on shutdown."""
    ctx = build_context()
    app.state.ctx = ctx
    await orchestrator.recover(ctx)
    task = None
    if ctx.settings.api_key:
        task = asyncio.create_task(poller.run(ctx, lambda m: orchestrator.dispatch(ctx, m)))
    else:
        emit(ctx, "error", "app", payload={"where": "start", "error": "PIXEL_API_KEY is not set: not polling"})
    yield
    for running in (task, *ctx.tasks):
        if running:
            running.cancel()
    if task:
        with suppress(asyncio.CancelledError):
            await task
    await ctx.mailbox.client.aclose()
    ctx.db.close()


app = FastAPI(title="PIXEL", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(dashboard.router)
