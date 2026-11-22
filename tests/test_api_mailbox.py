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
    raw = json.loads((MAILS / "bid-airline-priced.json").read_text())
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
    assert mapping_gaps(raw) == ["from", "to", "received_at"]


def test_a_captured_mail_maps_without_gaps():
    raw = json.loads((MAILS / "bid-decline-lane.json").read_text())
    assert mapping_gaps(raw) == []
    assert to_message(raw, "in").sent_at.isoformat() == raw["received_at"]


@pytest.mark.parametrize(
    ("change", "gap"),
    [
        ({"received_at": "Sun, 22 Nov 2026 01:00:00 +0000"}, None),
        ({"received_at": 1795309200000}, None),
        ({"received_at": "1795309200"}, None),
        ({"received_at": "yesterday-ish"}, "received_at (unreadable)"),
        ({"references": ["<abc@host>", 9]}, "references (unreadable)"),
        ({"in_reply_to": "<abc@host>"}, "in_reply_to (unreadable)"),
        ({"attachments": [{"filename": "list.pdf"}, {"id": 3, "filename": "ok.pdf"}]}, "attachments (without id)"),
    ],
)
@respx.mock
async def test_one_mail_in_an_unreadable_shape_never_stops_the_poll(config, change, gap):
    good = {"id": 10, "from": "a@b.c", "to": "us@x", "subject": "s", "body": "b", "received_at": "2026-11-22T01:00:00Z"}
    odd = {**good, "id": 11, **change}
    respx.get("https://api.test/v1/mailbox/messages").mock(
        return_value=httpx.Response(200, json={"messages": [good, odd, {**good, "id": 12}]})
    )
    messages, next_since_id = await Mailbox(make_client(config)).poll(0)
    assert [m.id for m in messages] == [10, 11, 12]
    assert next_since_id == 12
    assert messages[1].sent_at.year == 2026 or gap == "received_at (unreadable)"
    assert (gap in mapping_gaps(odd)) if gap else mapping_gaps(odd) == []


@respx.mock
async def test_a_mail_without_an_id_is_skipped_and_listed(config):
    good = {"id": 10, "from": "a@b.c", "to": "us@x", "subject": "s", "body": "b", "received_at": "2026-11-22T01:00:00Z"}
    respx.get("https://api.test/v1/mailbox/messages").mock(
        return_value=httpx.Response(200, json={"messages": [good, {"id": None, "subject": "?"}, {**good, "id": 12}]})
    )
    mailbox = Mailbox(make_client(config))
    messages, next_since_id = await mailbox.poll(0)
    assert [m.id for m in messages] == [10, 12]
    assert next_since_id == 12
    assert mailbox.skipped == [{"id": None, "error": "TypeError"}]


@pytest.mark.parametrize("status", [429, 503])
@respx.mock
async def test_an_attachment_refused_for_a_moment_is_fetched_again(config, monkeypatch, status):
    monkeypatch.setattr("pixel.api.mailbox.ATTACHMENT_WAIT_S", 0.0)
    route = respx.get("https://api.test/v1/mailbox/messages/7/attachments/1").mock(
        side_effect=[httpx.Response(status, headers={"Retry-After": "0"}), httpx.Response(200, content=b"%PDF")]
    )
    assert await Mailbox(make_client(config)).attachment(7, 1) == b"%PDF"
    assert route.call_count == 2


@respx.mock
async def test_an_attachment_that_stays_refused_raises_after_a_few_tries(config, monkeypatch):
    monkeypatch.setattr("pixel.api.mailbox.ATTACHMENT_WAIT_S", 0.0)
    route = respx.get("https://api.test/v1/mailbox/messages/7/attachments/1").mock(return_value=httpx.Response(429))
    with pytest.raises(ApiError):
        await Mailbox(make_client(config)).attachment(7, 1)
    assert route.call_count == 3
