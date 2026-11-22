"""Replay stored mail through the pipeline without sending anything.

uv run python -m tools.replay tests/fixtures/mails/<file>.json     # one mail
uv run python -m tools.replay --session 123                        # a whole session from the database
uv run python -m tools.replay --capture 123 > tests/fixtures/mails/rfq-door.json
uv run python -m tools.replay --db data/pixel.sqlite3 --session 123    # with the stored tariffs, carriers and bids

A replay runs dry and with an API client that refuses every call unless `--online` is given (then portal
and gateway are real, mail still is not, and the scorer counts the spend in that 18-minute window, R28). It runs
on a fresh database, or with `--db` on a copy of that database, never on the file itself: tariffs, carriers, zones
and the mails we sent take part, and what the replayed mails left behind (their sessions, quotes and events) is
removed from the copy first. The mail's stored time is moved to now, so its deadline starts with the replay.
"""

import argparse
import asyncio
import base64
import dataclasses
import json
import selectors
import sqlite3
import tempfile
from contextvars import copy_context
from datetime import timedelta
from pathlib import Path

import httpx

from pixel.api.mailbox import Mailbox, to_message
from pixel.api.portal import BookingUnavailable
from pixel.app import build_context
from pixel.config import settings
from pixel.contracts import Event, Message
from pixel.core import events, orchestrator, poller, timing


class OfflineMailbox(Mailbox):
    """Serves captured attachments and outbound receipts locally."""

    def __init__(self, client: httpx.AsyncClient, files: dict[tuple[int, int], bytes]):
        super().__init__(client)
        self.files = files
        self.sent = 0

    async def poll(self, since_id: int, limit: int = 200):
        return [], since_id

    async def send(self, to, subject, body, in_reply_to=None, attachments=()) -> Message:
        """Reached only in an `as_live` replay: the mail gets an id no stored mail has and goes nowhere."""
        self.sent -= 1
        raw = {
            "id": self.sent,
            "received_at": timing.now().isoformat(),
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
        }
        return to_message(raw, "out")

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


def _copy(source: str, target: str, raws: list[dict]) -> int:
    """Copies a database and forgets the replayed mails and their sessions. Returns the copy's last event id."""
    original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn = sqlite3.connect(target)
    original.backup(conn)
    original.close()
    ids = [(int(raw["id"]),) for raw in raws]
    sessions = [conn.execute("SELECT session_id FROM messages WHERE id = ?", i).fetchone() or i for i in ids]
    conn.executemany("DELETE FROM messages WHERE id = ? AND direction = 'in'", ids)
    for table in ("events", "quotes", "bookings", "shipments"):
        conn.executemany(f"DELETE FROM {table} WHERE session_id = ?", sessions)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "mail_work" in tables:
        conn.executemany("DELETE FROM mail_work WHERE message_id = ?", ids)
    if "mail_delivery" in tables:
        conn.executemany("DELETE FROM mail_delivery WHERE session_id = ?", sessions)
    conn.executemany("DELETE FROM sessions WHERE id = ?", sessions)
    conn.executemany("DELETE FROM kv WHERE key = 'quote:' || ?", sessions)
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    conn.commit()
    conn.close()
    return last


async def _no_booking(rate_id: str, team_reference: str) -> None:
    raise BookingUnavailable("replay: no portal booking")


class _ReplaySelector(selectors.DefaultSelector):
    """On an offline event loop, the next timer is the next input. Skip the idle interval."""

    elapsed = 0.0

    def select(self, timeout=None):
        if timeout:
            self.elapsed += timeout
        return super().select(0 if timeout is not None else None)


class _ReplayLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        self.replay_selector = _ReplaySelector()
        super().__init__(self.replay_selector)

    def time(self):
        return super().time() + self.replay_selector.elapsed

    def run_in_executor(self, executor, func, *args):
        """A thread's work would let the skipped timers outrun it; on the replay clock it takes no time."""
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as e:  # noqa: BLE001  handed to the awaiting caller, as a thread would
            future.set_exception(e)
        return future


def _offline_run(raws: list[dict], db: str | None, as_live: bool) -> list[Event]:
    with asyncio.Runner(loop_factory=_ReplayLoop) as runner:
        loop = runner.get_loop()
        start, baseline = to_message(raws[0], "in").sent_at, loop.time()
        token = timing.clock.set(lambda: start + timedelta(seconds=loop.time() - baseline))
        try:
            # Runner snapshots its context when get_loop initializes it.
            return runner.run(_run(raws, False, db, as_live), context=copy_context())
        finally:
            timing.clock.reset(token)


async def run(raws: list[dict], online: bool, db: str | None = None, as_live: bool = False) -> list[Event]:
    """Offline replay advances an isolated timer clock, preserving the order of bids and deadlines."""
    if not online:
        return await asyncio.to_thread(_offline_run, raws, db, as_live)
    return await _run(raws, True, db, as_live)


async def _run(raws: list[dict], online: bool, db: str | None, as_live: bool) -> list[Event]:
    config = dataclasses.replace(
        settings(), dry_run=not as_live, db_path=str(Path(tempfile.mkdtemp()) / "replay.sqlite3")
    )
    last_event = _copy(db, config.db_path, raws) if db else 0
    ctx = build_context(config)
    original = ctx.mailbox.client
    client = (
        original if online else httpx.AsyncClient(base_url=config.api_base, transport=httpx.MockTransport(_offline))
    )
    ctx.mailbox = OfflineMailbox(client, _files(raws))
    # Captured replies arrive on schedule. The offline clock also exercises the full production deadline.
    ctx.mailbox.late_mail = not online or len(raws) > 1
    ctx.portal.book = _no_booking  # `as_live` is not dry, but a replay must never book a replayed acceptance
    if not online:
        ctx.portal.client = ctx.gateway.client = ctx.board.client = client
    first = to_message(raws[0], "in").sent_at

    async def feed(raw: dict, wait: bool) -> None:
        message = to_message(raw, "in")
        if wait:
            await asyncio.sleep((message.sent_at - first).total_seconds())
        stored = poller.ingest(ctx, message.model_copy(update={"sent_at": timing.now()}))
        if stored:
            await orchestrator.dispatch(ctx, stored)

    later = []
    try:
        if as_live:
            later = [asyncio.create_task(feed(raw, wait=True)) for raw in raws[1:]]
            await feed(raws[0], wait=False)
        else:
            for raw in raws:
                await feed(raw, wait=False)
        return events.since(ctx, last_event, limit=10_000)
    finally:
        for task in later:
            task.cancel()
        await asyncio.gather(*later, return_exceptions=True)
        await original.aclose()
        if client is not original:
            await client.aclose()
        ctx.db.close()


def raws_of(fixture: dict | list[dict]) -> list[dict]:
    """A fixture is one mail, or a list: the request first, then the carriers' answers at their own times. An
    answer that lists the request in `references` joins its session, as a reply to our bid request does live."""
    return fixture if isinstance(fixture, list) else [fixture]


async def replay(raws: list[dict], online: bool, db: str | None = None) -> int:
    for event in await run(raws, online, db):
        usage = {"tokens_in": event.tokens_in, "tokens_out": event.tokens_out, "cost_usd": event.cost_usd,
                 "ms": event.duration_ms} if event.kind == "llm.call" else {}  # fmt: skip
        print(json.dumps({**event.payload, **usage, "ts": event.ts.isoformat(timespec="seconds"),
                          "session": event.session_id, "event": event.kind, "actor": event.actor}, default=str))  # fmt: skip
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fixture", nargs="?", type=Path)
    parser.add_argument("--session", type=int, help="replay every incoming mail of a stored session")
    parser.add_argument("--capture", type=int, help="print one stored mail as the API returned it")
    parser.add_argument("--online", action="store_true", help="real portal and gateway; mail stays dry")
    parser.add_argument("--db", help="replay on a copy of this database instead of a fresh one")
    args = parser.parse_args()
    db_path = args.db or settings().db_path
    if args.capture:
        print(json.dumps(_stored(db_path, "SELECT data FROM messages WHERE id = ?", args.capture)[0], indent=2))
        return
    if args.session:
        raws = _stored(db_path, "SELECT data FROM messages WHERE session_id = ? AND direction = 'in' ORDER BY id",
                       args.session)  # fmt: skip
    elif args.fixture:
        raws = raws_of(json.loads(args.fixture.read_text()))
    else:
        parser.error("a fixture, --session or --capture")
    raise SystemExit(asyncio.run(replay(raws, args.online, args.db)))


if __name__ == "__main__":
    main()
