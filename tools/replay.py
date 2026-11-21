"""Replay stored mail through the pipeline without sending anything.

uv run python -m tools.replay tests/fixtures/mails/<file>.json     # one mail
uv run python -m tools.replay --session 123                        # a whole session from the database
uv run python -m tools.replay --capture 123 > tests/fixtures/mails/rfq-door.json

A replay always runs dry, on a fresh database, and with an API client that refuses every call unless `--online`
is given (then portal and gateway are real, mail still is not). The mail's stored time is moved to now, so its
deadline starts with the replay.
"""

import argparse
import asyncio
import base64
import dataclasses
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pixel.api.client import make_client
from pixel.api.mailbox import Mailbox, to_message
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Message
from pixel.core import events, orchestrator, poller


class OfflineMailbox(Mailbox):
    """Serves attachments from the fixture (`attachments[].content_base64`), sends nothing."""

    def __init__(self, client: httpx.AsyncClient, files: dict[tuple[int, int], bytes]):
        super().__init__(client)
        self.files = files

    async def poll(self, since_id: int, limit: int = 200):
        return [], since_id

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.files[(message_id, attachment_id)]


def _offline(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"detail": "replay is offline"})


def _files(raws: list[dict]) -> dict[tuple[int, int], bytes]:
    return {
        (int(raw["id"]), int(a["id"])): base64.b64decode(a["content_base64"])
        for raw in raws
        for a in raw.get("attachments") or ()
        if a.get("content_base64")
    }


def _stored(db_path: str, query: str, value: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(query, (value,)).fetchall()
    conn.close()
    return [json.loads(data)["raw"] for (data,) in rows]


async def replay(raws: list[dict], online: bool) -> int:
    config = dataclasses.replace(settings(), dry_run=True, db_path=str(Path(tempfile.mkdtemp()) / "replay.sqlite3"))
    ctx = build_context(config)
    client = make_client(config) if online else httpx.AsyncClient(transport=httpx.MockTransport(_offline))
    ctx.mailbox = OfflineMailbox(client, _files(raws))
    if not online:
        ctx.portal.client = ctx.gateway.client = ctx.board.client = client
    session_id = None
    for raw in raws:
        message: Message = to_message(raw, "in").model_copy(update={"sent_at": datetime.now(UTC)})
        stored = poller.ingest(ctx, message)
        if stored:
            session_id = session_id or stored.session_id
            await orchestrator.dispatch(ctx, stored)
    for event in events.since(ctx, 0, limit=10_000):
        print(json.dumps({**event.payload, "ts": event.ts.isoformat(timespec="seconds"), "session": event.session_id,
                          "event": event.kind, "actor": event.actor}, default=str))  # fmt: skip
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixture", nargs="?", type=Path)
    parser.add_argument("--session", type=int, help="replay every incoming mail of a stored session")
    parser.add_argument("--capture", type=int, help="print one stored mail as the API returned it")
    parser.add_argument("--online", action="store_true", help="real portal and gateway; mail stays dry")
    args = parser.parse_args()
    db_path = settings().db_path
    if args.capture:
        print(json.dumps(_stored(db_path, "SELECT data FROM messages WHERE id = ?", args.capture)[0], indent=2))
        return
    if args.session:
        raws = _stored(db_path, "SELECT data FROM messages WHERE session_id = ? AND direction = 'in' ORDER BY id",
                       args.session)  # fmt: skip
    elif args.fixture:
        raws = [json.loads(args.fixture.read_text())]
    else:
        parser.error("a fixture, --session or --capture")
    raise SystemExit(asyncio.run(replay(raws, args.online)))


if __name__ == "__main__":
    main()
