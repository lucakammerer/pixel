"""The last reader before a quote leaves (`agents/rfq/unmet.py`), on a fake model."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_check import PLAIN, airline, quote, rfq
from test_outbox import make_ctx

from pixel import llm
from pixel.agents.rfq import unmet
from pixel.contracts import LlmResult
from pixel.core import feedback, timing
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes

REFERENCE = "Please confirm that you will book it under our reference PO 4711."
REQUEST = f"{PLAIN}\n{REFERENCE}"
INSURANCE = "Please include cargo insurance in the all-in price."
OPTION = airline("1483.43")
CASES = json.loads((Path(__file__).parent / "fixtures" / "unmet-asks.json").read_text(encoding="utf-8"))["cases"]


def ask(kind: str, sentence: str) -> dict:
    return {"class": kind, "sentence": sentence}


def model(monkeypatch, *asks: dict) -> tuple[list[dict], list[dict]]:
    asked, posted = [], []

    async def ask_json(ctx, **kw):
        asked.append(kw)
        return LlmResult(data={"unmet": list(asks)}, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    async def post(ctx, issues):
        posted.extend(issues)

    monkeypatch.setattr(llm, "ask_json", ask_json)
    monkeypatch.setattr(feedback, "post", post)
    return asked, posted


def deadline(target_s: float = 30) -> Deadline:
    now = timing.now()
    return Deadline(now, now + timedelta(seconds=target_s), now + timedelta(seconds=300), now + timedelta(seconds=360))


async def settle(ctx, request: str = REQUEST, target_s: float = 30, by_s: float = 60):
    drafted = quote(rfq(), OPTION)
    by = timing.now() + timedelta(seconds=by_s)
    await unmet.settle(ctx, drafted, request, deadline(target_s), by)
    await asyncio.gather(*ctx.tasks)
    return drafted


def recorded(conn) -> dict:
    row = conn.execute("SELECT payload FROM events WHERE kind = 'quote.unmet' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0])


async def test_a_wording_ask_never_adds_text_to_the_quote_it_alerts_and_the_quote_leaves_at_once(conn, monkeypatch):
    added = "Cargo insurance is included in the all-in price."  # a service we did not price
    asked, posted = model(monkeypatch, ask("wording", INSURANCE) | {"add": added})
    ctx = make_ctx(conn, unmet="on", alert_url="http://alert")
    started = timing.now()
    drafted = await settle(ctx, f"{PLAIN}\n{INSURANCE}", target_s=20)
    assert INSURANCE in asked[0]["user"] and drafted.body in asked[0]["user"] and "offer-Q-1.pdf" in asked[0]["user"]
    assert quotes.for_session(ctx, 1) is None and recorded(conn)["asks"] == [ask("wording", INSURANCE)]
    assert [(p["issue"], p["classes"], p["held"]) for p in posted] == [("unmet_ask", ["wording"], False)]
    assert (timing.now() - started).total_seconds() < 5


async def test_an_ask_that_moves_the_price_is_never_guessed_it_alerts_by_code_and_holds_until_the_target(
    conn, monkeypatch
):
    _, posted = model(monkeypatch, ask("price", INSURANCE))
    ctx = make_ctx(conn, unmet="on", alert_url="http://alert")
    started = timing.now()
    await settle(ctx, f"{PLAIN}\n{INSURANCE}", target_s=0.6)
    assert (timing.now() - started).total_seconds() >= 0.6
    assert posted[0] | {"at": ""} == {"issue": "unmet_ask", "session": 1, "at": "", "classes": ["price"], "held": True}
    assert recorded(conn)["hold_until"] and recorded(conn)["asks"][0]["sentence"] == INSURANCE
    assert [i["issue"] for i in feedback.attention(conn, 5)] == ["unmet_ask"]


async def test_reading_and_holding_end_before_the_orchestrator_takes_the_request_over(conn, monkeypatch):
    _, posted = model(monkeypatch, ask("price", INSURANCE))
    started = timing.now()
    await settle(make_ctx(conn, unmet="on", alert_url="http://alert"), f"{PLAIN}\n{INSURANCE}", target_s=20, by_s=0.5)
    assert (timing.now() - started).total_seconds() < 2 and posted[0]["held"]

    async def hangs(ctx, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(llm, "ask_json", hangs)
    conn.execute("DELETE FROM events")
    started = timing.now()
    await settle(make_ctx(conn, unmet="on"), target_s=20, by_s=0.5)
    assert (timing.now() - started).total_seconds() < 2


async def test_a_person_who_takes_the_session_over_ends_the_hold(conn, monkeypatch):
    model(monkeypatch, ask("scope", PLAIN))
    ctx = make_ctx(conn, unmet="on")
    conn.execute("INSERT INTO sessions (id, customer, state, received_at, mode) VALUES (1, 'c@x', 'open', '', 'human')")
    started = timing.now()
    await settle(ctx, target_s=20)
    assert (timing.now() - started).total_seconds() < 5


async def test_a_sentence_the_request_does_not_hold_moves_nothing(conn, monkeypatch):
    _, posted = model(monkeypatch, ask("selection", "Please do not offer Ghost Air."))
    started = timing.now()
    await settle(make_ctx(conn, unmet="on", alert_url="http://alert"), target_s=20)
    assert posted == [] and recorded(conn)["asks"] == []
    assert (timing.now() - started).total_seconds() < 5


async def test_a_malformed_answer_is_an_error_event_and_the_quote_leaves(conn, monkeypatch):
    model(monkeypatch, {"class": "price"})
    await settle(make_ctx(conn, unmet="on"), target_s=20)
    assert json.loads(conn.execute("SELECT payload FROM events WHERE kind = 'error'").fetchone()[0])["where"] == "unmet"


@pytest.mark.parametrize(("mode", "calls"), [("log", 1), ("off", 0)])
async def test_log_records_without_holding_or_alerting_and_off_asks_nothing(conn, monkeypatch, mode, calls):
    asked, posted = model(monkeypatch, ask("wording", REFERENCE), ask("price", PLAIN))
    ctx = make_ctx(conn, unmet=mode, alert_url="http://alert")
    started = timing.now()
    await settle(ctx, target_s=20)
    await settle(ctx, target_s=20)  # a restart hands the request to its handler again: no second call
    assert posted == [] and len(asked) == calls
    assert (timing.now() - started).total_seconds() < 5
    assert not calls or recorded(conn)["open"] == ["price", "wording"]
    assert feedback.attention(conn, 5) == []  # an unmeasured reader's finding is no issue for the repair run


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_the_stored_rounds_expect_only_what_the_reader_can_return(conn, monkeypatch, case):
    """`tools.bench_unmet` scores the prompt on these: an expected sentence the request does not hold, or a class
    outside the list, would count a right answer as wrong."""
    expected = [ask(e["class"], e.get("sentence", case["request"].splitlines()[0])) for e in case["expected"]]
    model(monkeypatch, *expected)
    found = await unmet.read(make_ctx(conn), case["request"], case["subject"], case["reply"], (), 1, None)
    assert found == expected
