"""The browser's side of a voice call:
- Order status demo calls: start ringing (`/api/calls/order`), pick up on the fake phone screen (`/phone` +
  `/api/calls/{id}/accept`), stream live turns (`/api/calls/{id}/turn`) and status updates (`/api/calls/stream`)
  to the laptop audience screen.
- Late carrier / missing customer fact calls: start directly (`/api/calls`), relay answers (`/api/calls/{id}/answer`).

The ElevenLabs API key never leaves the server."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from pixel.core import events
from pixel.voice import calls

router = APIRouter()
VOICE_DIR = Path(__file__).parent
MAX_BODY = 16 * 1024
CALL_STREAM_KINDS = {
    "call.ringing",
    "call.started",
    "call.connected",
    "call.turn",
    "call.answer",
    "call.ended",
    "call.transcript",
    "shipment.updated",
}


async def _json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise calls.CallError(413, "Request is too large.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise calls.CallError(400, "Request is not valid JSON.") from None
    if not isinstance(data, dict):
        raise calls.CallError(400, "Request is not a JSON object.")
    return data


def _failed(e: calls.CallError) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=e.status)


@router.get("/voice/call.js", include_in_schema=False)
async def script():
    return FileResponse(VOICE_DIR / "call.js", media_type="text/javascript")


@router.get("/voice/phone.js", include_in_schema=False)
async def phone_script():
    return FileResponse(VOICE_DIR / "phone.js", media_type="text/javascript")


@router.get("/voice/order_call.js", include_in_schema=False)
async def order_call_script():
    return FileResponse(VOICE_DIR / "order_call.js", media_type="text/javascript")


@router.get("/phone", response_class=HTMLResponse, include_in_schema=False)
async def phone_screen(call_id: int | None = None, embed: int = 0):
    """Simulated iPhone for the live pitch demo (`phone.html`): a lock screen that rings on `call.ringing` events
    from `/api/calls/stream`, the incoming-call screen, and the in-call screen that runs the ElevenLabs conversation.
    On a real phone it fills the screen; elsewhere it draws the device. No transcript: the laptop shows that."""
    html = (VOICE_DIR / "phone.html").read_text(encoding="utf-8")
    html = html.replace("__BODY_CLASS__", "embed" if embed else "")
    html = html.replace("__INITIAL_CALL__", str(call_id) if call_id is not None else "")
    return HTMLResponse(html)


@router.get("/api/calls/stream")
async def stream(request: Request):
    """Server-Sent Events stream of `call.*` and `shipment.updated` events for both the fake phone screen
    and the laptop audience live monitor."""
    async def gen():
        aiter = events.subscribe()
        pending = asyncio.create_task(aiter.__anext__())
        await asyncio.sleep(0)  # ensure subscription is registered before emitting `: ok`
        yield ": ok\n\n"
        try:
            while not await request.is_disconnected():
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                ev = pending.result()
                pending = asyncio.create_task(aiter.__anext__())
                if ev.kind not in CALL_STREAM_KINDS:
                    continue
                data = {
                    "id": ev.id,
                    "kind": ev.kind,
                    "session_id": ev.session_id,
                    "ts": ev.ts.isoformat(),
                    "payload": ev.payload,
                }
                yield f"data: {json.dumps(data, default=str)}\n\n"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await aiter.aclose()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/calls/order")
async def start_order(request: Request):
    """Starts ringing the fake phone screen for a specific order/shipment."""
    try:
        data = await _json(request)
        return JSONResponse(calls.ring_order(request.app.state.ctx, str(data.get("shipment_id", ""))), 201)
    except calls.CallError as e:
        return _failed(e)


@router.get("/api/calls/ringing")
async def ringing(request: Request, call_id: int | None = None):
    found = calls.active_ringing(request.app.state.ctx, call_id)
    return JSONResponse(found or {"ringing": False})


@router.get("/api/calls/{call_id}/state")
async def state(request: Request, call_id: int):
    try:
        return JSONResponse(calls.call_state(request.app.state.ctx, call_id))
    except calls.CallError as e:
        return _failed(e)


@router.post("/api/calls/{call_id}/accept")
async def accept(request: Request, call_id: int):
    try:
        return JSONResponse(await calls.accept_order(request.app.state.ctx, call_id))
    except calls.CallError as e:
        return _failed(e)


@router.post("/api/calls/{call_id}/turn")
async def turn(request: Request, call_id: int):
    try:
        data = await _json(request)
        calls.record_turn(request.app.state.ctx, call_id, str(data.get("role", "user")), str(data.get("message", "")))
    except calls.CallError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/api/calls")
async def start(request: Request):
    ctx = request.app.state.ctx
    try:
        data = await _json(request)
        return JSONResponse(await calls.start(ctx, int(data.get("session_id") or 0), str(data.get("email", ""))), 201)
    except calls.CallError as e:
        return _failed(e)


@router.post("/api/calls/{call_id}/connected")
async def connected(request: Request, call_id: int):
    try:
        calls.connected(request.app.state.ctx, call_id, str((await _json(request)).get("conversation_id", "")))
    except calls.CallError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/api/calls/{call_id}/answer")
async def answer(request: Request, call_id: int):
    """The agent's client tool, relayed by the browser while the call is going on."""
    try:
        result = calls.answer(request.app.state.ctx, call_id, await _json(request))
    except calls.CallError as e:
        return _failed(e)
    return result if isinstance(result, dict) else {"message_id": result}


@router.post("/api/calls/{call_id}/ended")
async def ended(request: Request, call_id: int):
    try:
        calls.ended(request.app.state.ctx, call_id, str((await _json(request)).get("error", "")))
    except calls.CallError as e:
        return _failed(e)
    return {"ok": True}


@router.post("/api/calls/{call_id}/transcript")
async def transcript(request: Request, call_id: int):
    try:
        found = await calls.transcript(request.app.state.ctx, call_id)
    except calls.CallError as e:
        return _failed(e)
    return JSONResponse(found or {"pending": True}, status_code=200 if found else 202)
