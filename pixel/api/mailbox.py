"""GET/POST /v1/mailbox/messages and the push subscription. Maps the API's mail shape to `contracts.Message`."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from pixel.api.client import request
from pixel.contracts import Attachment, Message

# Fixed addresses named in the API docs, not in a dataset.
RATEDESK = "ratedesk@hackloop.mail"
TRACKING = "tracking@hackloop.mail"

ATTACHMENT_TYPES = ("pdf", "xlsx", "xls", "csv", "txt", "png", "jpg", "docx")  # R6
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _time(value: Any) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


SENDER = ("from", "sender", "from_address")
RECIPIENT = ("to", "recipient", "to_address")
STORED_AT = ("received_at", "created_at", "sent_at", "stored_at", "timestamp", "date")


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((raw[k] for k in keys if raw.get(k) is not None), None)


def mapping_gaps(raw: dict[str, Any]) -> list[str]:
    """Fields `to_message` had to guess because the mail did not carry them under any name we know."""
    return [keys[0] for keys in (SENDER, RECIPIENT, STORED_AT) if _first(raw, keys) is None]


def to_message(raw: dict[str, Any], direction: str) -> Message:
    """The only place that knows the API's mail fields. `session_id` is set by the poller. Tolerant on purpose: a
    mail it cannot fully read still gets a deadline (from now) instead of blocking the mailbox; see `mapping_gaps`."""
    stored_at = _first(raw, STORED_AT)
    return Message(
        id=int(raw["id"]),
        session_id=int(raw["id"]),
        direction=direction,
        sender=str(_first(raw, SENDER) or "").strip().lower(),
        to=str(_first(raw, RECIPIENT) or "").strip().lower(),
        subject=raw.get("subject") or "",
        body=raw.get("body") or "",
        in_reply_to=raw.get("in_reply_to"),
        references=tuple(int(r) for r in raw.get("references") or ()),
        attachments=tuple(
            Attachment(
                id=int(a["id"]),
                filename=a.get("filename", ""),
                content_type=a.get("content_type", ""),
                size=int(a.get("size") or 0),
            )
            for a in raw.get("attachments") or ()
        ),
        sent_at=_time(stored_at) if stored_at is not None else datetime.now(UTC),
        raw=raw,
    )


def check_attachments(attachments: tuple[tuple[str, str, bytes], ...]) -> None:
    """R6, before the API answers 422."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"at most {MAX_ATTACHMENTS} attachments")
    for filename, _, content in attachments:
        if filename.rsplit(".", 1)[-1].lower() not in ATTACHMENT_TYPES:
            raise ValueError(f"attachment type not allowed: {filename}")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachment over 10 MB: {filename}")


def signed(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """A push's `X-Webhook-Signature`: `sha256=` + hex HMAC-SHA256 of timestamp + `.` + raw body."""
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Mailbox:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def poll(self, since_id: int, limit: int = 200) -> tuple[list[Message], int]:
        """Mail to us with id > since_id, oldest first, and `next_since_id`."""
        response = await request(
            self.client, "GET", "/v1/mailbox/messages", params={"since_id": since_id, "limit": limit}
        )
        body = response.json()
        messages = [to_message(raw, "in") for raw in body.get("messages") or body.get("items") or []]
        next_since_id = body.get("next_since_id") or max((m.id for m in messages), default=since_id)
        return messages, int(next_since_id)

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: int | None = None,
        attachments: tuple[tuple[str, str, bytes], ...] = (),
    ) -> Message:
        """Raw send. Everything except `core/outbox.py` goes through the outbox instead."""
        check_attachments(attachments)
        payload: dict[str, Any] = {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        if attachments:
            payload["attachments"] = [
                {"filename": f, "content_type": t, "content_base64": base64.b64encode(c).decode()}
                for f, t, c in attachments
            ]
        response = await request(self.client, "POST", "/v1/mailbox/messages", json=payload)
        return to_message(response.json(), "out")

    async def get(self, message_id: int) -> Message:
        response = await request(self.client, "GET", f"/v1/mailbox/messages/{message_id}")
        return to_message(response.json(), "in")

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        response = await request(self.client, "GET", f"/v1/mailbox/messages/{message_id}/attachments/{attachment_id}")
        return response.content

    async def subscribe(self, url: str, secret: str) -> None:
        """Every new mail is pushed to `url`, signed with `secret`. A second call replaces both."""
        await request(self.client, "PUT", "/v1/mailbox/webhook", json={"url": url, "secret": secret})

    async def webhook_test(self) -> dict[str, Any]:
        """One signed test call to our endpoint: `delivered`, `status_code`, `error`."""
        return (await request(self.client, "POST", "/v1/mailbox/webhook/test")).json()

    async def webhook(self) -> dict[str, Any]:
        """Our subscription and the last 20 pushes."""
        return (await request(self.client, "GET", "/v1/mailbox/webhook")).json()
