import dataclasses
import json
from pathlib import Path

import httpx
import pytest
import respx

from pixel.api.client import ApiError, make_client
from pixel.api.mailbox import Mailbox, mapping_gaps, to_message
from pixel.config import settings

MAILS = Path(__file__).parent / "fixtures" / "mails"
KEY = "marker-key-0123456789"


@pytest.fixture
def config():
    return dataclasses.replace(settings(), api_base="https://api.test", api_key=KEY)


@respx.mock
async def test_poll_maps_mail_and_sends_the_key_header(config):
    raw = json.loads((MAILS / "synthetic-bid.json").read_text())
    route = respx.get("https://api.test/v1/mailbox/messages").mock(
        return_value=httpx.Response(200, json={"messages": [raw], "next_since_id": raw["id"]})
    )
    messages, next_since_id = await Mailbox(make_client(config)).poll(0)
    assert route.calls[0].request.headers["X-API-Key"] == KEY
    assert next_since_id == raw["id"]
    assert messages[0].in_reply_to == raw["in_reply_to"]
    assert messages[0].references == tuple(raw["references"])
    assert messages[0].sent_at.tzinfo is not None


@respx.mock
async def test_get_retries_a_5xx_then_raises_typed(config, monkeypatch):
    monkeypatch.setattr("pixel.api.client.BACKOFF_S", (0, 0))
    route = respx.get("https://api.test/v1/mailbox/messages").mock(
        return_value=httpx.Response(502, json={"detail": "bad gateway"})
    )
    with pytest.raises(ApiError) as e:
        await Mailbox(make_client(config)).poll(0)
    assert route.call_count == 3
    assert e.value.status == 502
    assert KEY not in str(e.value)


@respx.mock
async def test_send_is_not_repeated_after_a_5xx(config):
    route = respx.post("https://api.test/v1/mailbox/messages").mock(return_value=httpx.Response(500))
    with pytest.raises(ApiError):
        await Mailbox(make_client(config)).send("x@y", "s", "b")
    assert route.call_count == 1


def test_a_mail_in_an_unknown_shape_still_maps_and_says_what_is_missing():
    raw = {"id": 7, "subject": "s", "body": "b"}
    assert to_message(raw, "in").id == 7
    assert mapping_gaps(raw) == ["from", "to", "created_at"]
