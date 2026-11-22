"""GET/POST /v1/mailbox/messages and the push subscription. Maps the API's mail shape to `contracts.Message`."""

import asyncio
import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

import httpx

from pixel.api.client import ApiError, request
from pixel.contracts import Attachment, Message

# Fixed addresses named in the API docs, not in a dataset.
RATEDESK = "ratedesk@hackloop.mail"
TRACKING = "tracking@hackloop.mail"

ATTACHMENT_TYPES = ("pdf", "xlsx", "xls", "csv", "txt", "png", "jpg", "docx")  # R6
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# A request that stands only in its attachment (rounds 65 to 68) is read without it when the download fails, so a
# 429 or 503 on it is waited out a few times, each wait at most ATTACHMENT_WAIT_S.
ATTACHMENT_TRIES = 3
ATTACHMENT_WAIT_S = 3.0


EPOCH_MS = 10**11  # an epoch above this is in milliseconds: in seconds it lies past the year 5000


def _time(value: Any) -> datetime:
    """ISO 8601, an RFC 2822 date header, or an epoch in seconds or milliseconds; ValueError for anything else."""
    text = str(value).strip()
    if isinstance(value, int | float) or re.fullmatch(r"\d+(?:\.\d+)?", text):
        number = float(text)
        return datetime.fromtimestamp(number / 1000 if number > EPOCH_MS else number, UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            raise ValueError(f"unreadable time {text[:40]!r}") from None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _readable_time(value: Any) -> datetime | None:
    try:
        return _time(value)
    except (ValueError, OverflowError, OSError):
        return None


def _int(value: Any) -> int | None:
    """An id as the API sends it, or None: a reference like "<abc@host>" names no mail we can look up."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


SENDER = ("from", "sender", "from_address")
RECIPIENT = ("to", "recipient", "to_address")
STORED_AT = ("received_at", "created_at", "sent_at", "stored_at", "timestamp", "date")


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((raw[k] for k in keys if raw.get(k) is not None), None)


def mapping_gaps(raw: dict[str, Any]) -> list[str]:
    """Fields `to_message` had to guess because the mail did not carry them under any name we know, or carried them
    in a shape it cannot read (a time, a reference, an attachment without its id)."""
    gaps = [keys[0] for keys in (SENDER, RECIPIENT, STORED_AT) if _first(raw, keys) is None]
    if (stored_at := _first(raw, STORED_AT)) is not None and _readable_time(stored_at) is None:
        gaps.append("received_at (unreadable)")
    if raw.get("in_reply_to") is not None and _int(raw["in_reply_to"]) is None:
        gaps.append("in_reply_to (unreadable)")
    if any(_int(r) is None for r in raw.get("references") or ()):
        gaps.append("references (unreadable)")
    if any(not isinstance(a, dict) or _int(a.get("id")) is None for a in raw.get("attachments") or ()):
        gaps.append("attachments (without id)")
    return gaps


def _address(value: object) -> str:
    """ "Kestrel Ops <ops@kestrel.example>" is ops@kestrel.example: carrier list, tracking and rate desk match by it."""
    text = str(value or "").strip()
    return (parseaddr(text)[1] or text).lower()


def to_message(raw: dict[str, Any], direction: str) -> Message:
    """The only place that knows the API's mail fields. `session_id` is set by the poller. Tolerant on purpose, field
    by field: a mail it cannot fully read still gets a deadline (from now) instead of blocking the mailbox; a
    reference or an attachment it cannot read is left out; see `mapping_gaps`. Only a mail without an id raises."""
    stored_at = _first(raw, STORED_AT)
    sent_at = _readable_time(stored_at) if stored_at is not None else None
    return Message(
        id=int(raw["id"]),
        session_id=int(raw["id"]),
        direction=direction,
        sender=_address(_first(raw, SENDER)),
        to=_address(_first(raw, RECIPIENT)),
        subject=raw.get("subject") or "",
        body=raw.get("body") or "",
        in_reply_to=_int(raw.get("in_reply_to")),
        references=tuple(r for r in map(_int, raw.get("references") or ()) if r is not None),
        attachments=tuple(
            Attachment(
                id=int(a["id"]),
                filename=str(a.get("filename") or ""),
                content_type=str(a.get("content_type") or ""),
                size=_int(a.get("size")) or 0,
            )
            for a in raw.get("attachments") or ()
            if isinstance(a, dict) and _int(a.get("id")) is not None
        ),
        sent_at=sent_at or datetime.now(UTC),
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
        self.skipped: list[dict[str, Any]] = []  # mails the last poll could not map at all; the poller reports them

    async def poll(self, since_id: int, limit: int = 200) -> tuple[list[Message], int]:
        """Mail to us with id > since_id, oldest first, and `next_since_id`. One mail that cannot be mapped is left
        out and listed in `skipped`, never the whole page: else every later request waits behind it for good."""
        response = await request(
            self.client, "GET", "/v1/mailbox/messages", params={"since_id": since_id, "limit": limit}
        )
        body = response.json()
        messages, self.skipped, ids = [], [], [since_id]
        for raw in body.get("messages") or body.get("items") or []:
            if isinstance(raw, dict) and (raw_id := _int(raw.get("id"))) is not None:
                ids.append(raw_id)
            try:
                messages.append(to_message(raw, "in"))
            except Exception as e:  # noqa: BLE001  one unreadable mail must not stop the mailbox (R1)
                self.skipped.append({"id": raw.get("id") if isinstance(raw, dict) else None, "error": type(e).__name__})
        next_since_id = _int(body.get("next_since_id")) or max(ids)
        return messages, next_since_id

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
        path = f"/v1/mailbox/messages/{message_id}/attachments/{attachment_id}"
        for attempt in range(1, ATTACHMENT_TRIES + 1):
            try:
                return (await request(self.client, "GET", path)).content
            except ApiError as e:
                if e.status not in (429, 503) or attempt == ATTACHMENT_TRIES:
                    raise
                await asyncio.sleep(min(e.retry_after or 1.0, ATTACHMENT_WAIT_S))
        raise AssertionError("unreachable")

    async def subscribe(self, url: str, secret: str) -> None:
        """Every new mail is pushed to `url`, signed with `secret`. A second call replaces both."""
        await request(self.client, "PUT", "/v1/mailbox/webhook", json={"url": url, "secret": secret})

    async def webhook_test(self) -> dict[str, Any]:
        """One signed test call to our endpoint: `delivered`, `status_code`, `error`."""
        return (await request(self.client, "POST", "/v1/mailbox/webhook/test")).json()

    async def webhook(self) -> dict[str, Any]:
        """Our subscription and the last 20 pushes."""
        return (await request(self.client, "GET", "/v1/mailbox/webhook")).json()
