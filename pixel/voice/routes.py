"""The browser's side of a voice call: start it, report its session and end, relay the agent's answer, fetch the
transcript. Behind the same login as the rest of the UI; the ElevenLabs key never leaves the server."""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from pixel.voice import calls

router = APIRouter()
SCRIPT = Path(__file__).with_name("call.js")
MAX_BODY = 16 * 1024


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
    return FileResponse(SCRIPT, media_type="text/javascript")


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
        message_id = calls.answer(request.app.state.ctx, call_id, await _json(request))
    except calls.CallError as e:
        return _failed(e)
    return {"message_id": message_id}


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
