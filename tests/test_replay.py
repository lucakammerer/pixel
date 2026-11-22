import sqlite3

import httpx
import pytest

from pixel import llm
from pixel.api.client import ApiError
from pixel.api.gateway import Gateway
from pixel.config import settings
from pixel.core import db
from pixel.core.context import Context
from tools import replay


def test_a_replay_on_a_copy_keeps_reference_data_and_forgets_the_replayed_session(tmp_path):
    source, target = str(tmp_path / "live.sqlite3"), str(tmp_path / "copy.sqlite3")
    live = db.connect(source)
    db.init(live)
    live.execute("INSERT INTO carriers VALUES ('Air One', 'airline', 'one@air', 'BBB')")
    for mail_id, direction in ((7, "in"), (8, "out"), (9, "in")):  # the request, our bid request, the carrier's bid
        live.execute("INSERT INTO messages VALUES (?, 7, ?, NULL, NULL, 'a', 'b', 's', 'b', NULL, 't', '{}')",
                     (mail_id, direction))  # fmt: skip
    live.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (7, 'a', 'quoted', 't')")
    live.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', 7, 'quote.sent', 'x', '{}')")
    live.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', 5, 'route', 'x', '{}')")
    live.execute("INSERT INTO kv VALUES ('quote:7', 'Q-1')")
    live.close()

    last = replay._copy(source, target, [{"id": 7}, {"id": 9}])

    copy = sqlite3.connect(target)
    count = lambda sql: copy.execute(sql).fetchone()[0]
    assert count("SELECT COUNT(*) FROM carriers") == 1
    assert [r[0] for r in copy.execute("SELECT id FROM messages")] == [8]  # the answer still finds our bid request
    assert count("SELECT COUNT(*) FROM sessions") + count("SELECT COUNT(*) FROM kv WHERE key = 'quote:7'") == 0
    assert [r[0] for r in copy.execute("SELECT session_id FROM events")] == [5] and last == 2
    live = sqlite3.connect(source)
    assert live.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


async def test_a_replay_answers_the_model_with_its_recorded_reading_and_a_changed_prompt_misses(conn):
    class Live:
        async def chat(self, payload):
            return {"choices": [{"message": {"content": '{"origin": "AAA"}'}}], "usage": {"completion_tokens": 9}}

    ctx = Context(settings=settings(), db=conn, mailbox=None, portal=None, gateway=Live(), board=None)
    ask = {"purpose": "rfq.extract", "system": "read the mail", "schema": {}, "session_id": 7}
    await llm.ask_json(ctx, user="FRA to ORD, 100 kg", **ask)

    client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(
        replay._recorded({"answers": replay.answers(conn, 7)})))  # fmt: skip
    ctx.gateway = Gateway(client)
    again = await llm.ask_json(ctx, user="FRA to ORD, 100 kg", **ask)
    assert again.data == {"origin": "AAA"} and again.cost_usd == 0
    with pytest.raises(ApiError):  # a changed prompt meets the offline gateway
        await llm.ask_json(ctx, user="FRA to ORD, 200 kg", **ask)
    await client.aclose()
