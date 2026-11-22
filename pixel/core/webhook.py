"""POST /hook: the mailbox pushes each new mail here the moment it arrives. The poller stays as the safety net."""

import json
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from pixel.api.mailbox import signed, to_message
from pixel.core import orchestrator, poller
from pixel.core.events import emit

router = APIRouter()
MAX_AGE_S = 300
PUSH_SLACK = timedelta(seconds=60)  # a push leaves the moment the mail is stored; its retries follow within seconds


@router.post("/hook", status_code=204)
async def hook(request: Request) -> None:
    """Refuses a push with a wrong signature or an old timestamp, then takes the mail the way a poll does: one the
    poll or an earlier push already stored is skipped by its id. Answers before the handlers run."""
    ctx = request.app.state.ctx
    secret = ctx.settings.webhook_secret
    body = await request.body()
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    if not secret or not signed(secret, timestamp, body, request.headers.get("X-Webhook-Signature", "")):
        raise HTTPException(401)
    if abs(time.time() - float(timestamp)) > MAX_AGE_S:
        raise HTTPException(401)
    payload = json.loads(body)
    if payload.get("event") != "mail.received":  # the test call sends a ping
        return
    try:
        message = to_message(payload["message"], "in")
    except (KeyError, TypeError, ValueError) as e:
        emit(ctx, "error", "webhook", payload={"where": "push", "error": repr(e)})
        return
    pushed_at = datetime.fromtimestamp(float(timestamp), UTC)
    poller.accept(ctx, message, lambda m: orchestrator.dispatch(ctx, m), pushed_at - PUSH_SLACK)
