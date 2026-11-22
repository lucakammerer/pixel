import dataclasses
import hashlib
import hmac
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pixel.config import settings
from pixel.core import orchestrator, webhook
from pixel.core.context import Context

MAIL = Path(__file__).parent / "fixtures" / "mails" / "rfq-door-no-origin.json"
SECRET = "test-secret-0123456789"


def push(client, body: bytes, secret: str = SECRET, age_s: int = 0):
    """Signed as the API docs say, not with our own `signed`."""
    timestamp = str(int(time.time()) - age_s)
    signature = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"X-Webhook-Timestamp": timestamp, "X-Webhook-Signature": signature}
    return client.post("/hook", content=body, headers=headers)


def test_a_signed_push_is_dispatched_once_and_a_forged_or_old_one_never(conn, monkeypatch):
    seen = []

    async def dispatch(ctx, message):
        seen.append(message.id)

    monkeypatch.setattr(orchestrator, "dispatch", dispatch)
    app = FastAPI()
    app.include_router(webhook.router)
    app.state.ctx = Context(dataclasses.replace(settings(), webhook_secret=SECRET), conn, None, None, None, None)
    raw = json.loads(MAIL.read_text())
    body = json.dumps({"event": "mail.received", "message": raw}).encode()
    with TestClient(app) as client:
        assert push(client, body, secret="another-secret-0123456789").status_code == 401
        assert push(client, body, age_s=3600).status_code == 401
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert push(client, body).status_code == 204
        assert push(client, body).status_code == 204  # a push can arrive twice
        assert push(client, b'{"event": "ping", "team": "t"}').status_code == 204
    assert seen == [raw["id"]]
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'mail.in'").fetchone()[0] == 1


def test_a_pushed_request_stamped_long_before_its_push_is_not_dropped_as_expired(conn, monkeypatch):
    async def dispatch(ctx, message):
        return None

    monkeypatch.setattr(orchestrator, "dispatch", dispatch)
    app = FastAPI()
    app.include_router(webhook.router)
    app.state.ctx = Context(dataclasses.replace(settings(), webhook_secret=SECRET), conn, None, None, None, None)
    raw = {**json.loads(MAIL.read_text()), "received_at": "2026-11-21T08:00:00+00:00"}  # a day before the push
    with TestClient(app) as client:
        assert push(client, json.dumps({"event": "mail.received", "message": raw}).encode()).status_code == 204
    stored = conn.execute("SELECT sent_at FROM messages WHERE id = ?", (raw["id"],)).fetchone()["sent_at"]
    assert abs(time.time() - webhook.PUSH_SLACK.total_seconds() - datetime.fromisoformat(stored).timestamp()) < 5
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'mail.clock'").fetchone()[0] == 1
