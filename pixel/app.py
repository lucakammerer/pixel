"""Process entry: `uv run uvicorn pixel.app:app`. One process runs poller, push endpoint, handlers and UI."""

import asyncio
import contextlib
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from pixel.agents.tariffs.agent import speech_model
from pixel.api.board import Board
from pixel.api.client import make_client
from pixel.api.gateway import Gateway
from pixel.api.mailbox import Mailbox
from pixel.api.portal import Portal
from pixel.config import Settings, settings
from pixel.core import db, feedback, orchestrator, poller, webhook
from pixel.core.context import Context
from pixel.core.events import emit
from pixel.ui import crm, dashboard, home, routes


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


def _preload_speech() -> None:
    with contextlib.suppress(Exception):  # a failed load is retried by the first voice message, which reports it
        speech_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """build_context, orchestrator.recover, then poller.run as a background task; cancels it on shutdown."""
    ctx = build_context()
    app.state.ctx = ctx
    await orchestrator.recover(ctx)
    task = None
    if ctx.settings.api_key and not ctx.settings.dry_run:
        # The speech model downloads ~145 MB on first use: never inside a voice message's deadline.
        threading.Thread(target=_preload_speech, daemon=True).start()
    if ctx.settings.api_key:
        task = asyncio.create_task(poller.run(ctx, lambda m: orchestrator.dispatch(ctx, m)))
        ctx.tasks.add(asyncio.create_task(feedback.run(ctx)))
    else:
        emit(ctx, "error", "app", payload={"where": "start", "error": "PIXEL_API_KEY is not set: not polling"})
    try:
        yield
    finally:
        running = set(ctx.tasks)
        if task:
            running.add(task)
        for handler in running:
            handler.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await ctx.mailbox.client.aclose()
        ctx.db.close()


app = FastAPI(title="PIXEL", lifespan=lifespan)
app.include_router(webhook.router)
app.include_router(home.router)
app.include_router(routes.router)
app.include_router(dashboard.router)
app.include_router(crm.router)
