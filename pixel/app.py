"""Process entry: `uv run uvicorn pixel.app:app`. One process runs the poller, the handlers and the UI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from pixel.ui import dashboard, routes


def build_context():
    """Settings, database (`db.init`), API clients -> Context."""
    raise NotImplementedError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """build_context, orchestrator.recover, then poller.run as a background task; cancels it on shutdown."""
    raise NotImplementedError
    yield


app = FastAPI(title="PIXEL", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(dashboard.router)
