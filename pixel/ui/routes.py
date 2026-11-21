"""Sessions UI: find a session, watch it live, step in. Reads the event log only."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def sessions(q: str = "", state: str = ""):
    """The list: newest first, searchable by customer, lane, reference; time left on open requests."""
    raise NotImplementedError


@router.get("/sessions/{session_id}")
async def session(session_id: int):
    """One session: mails, decisions, options with their prices, model calls with cost, the quote."""
    raise NotImplementedError


@router.get("/events")
async def stream(session_id: int | None = None, after_id: int = 0):
    """Server-sent events from `core.events.subscribe`, resumable with `after_id`."""
    raise NotImplementedError


@router.post("/sessions/{session_id}/takeover")
async def takeover(session_id: int):
    """mode = human: automatic mail for this session is held as drafts. The deadline fallback still sends."""
    raise NotImplementedError


@router.post("/sessions/{session_id}/release")
async def release(session_id: int):
    raise NotImplementedError


@router.post("/sessions/{session_id}/send")
async def send(session_id: int, subject: str, body: str):
    """A person sends a held draft or their own mail through `core.outbox`."""
    raise NotImplementedError
