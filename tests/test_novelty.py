import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pixel import llm
from pixel.agents.rfq import extract, novelty
from pixel.api.mailbox import to_message
from pixel.config import settings
from pixel.contracts import LlmResult, RateOption, Rfq, Source
from pixel.core import feedback
from pixel.core.context import Context
from pixel.pricing import selection, store

MAILS = Path(__file__).parent / "fixtures" / "mails"
READY = datetime(2026, 11, 27, tzinfo=UTC).date()


def request(name: str) -> tuple[str, str, Rfq]:
    message = to_message(json.loads((MAILS / name).read_text(encoding="utf-8")), "in")
    rfq = Rfq(session_id=1, message_id=1, customer=message.sender, origin="DUS", destination="JFK",
              weight_kg=Decimal(1540), received_at=message.sent_at, deadline_at=message.sent_at)  # fmt: skip
    return extract._text(message), message.subject, rfq


def option(carrier: str, total: int, days: int) -> RateOption:
    return RateOption(source=Source.SHEET, carrier=carrier, origin="DUS", destination="JFK",
                      chargeable_kg=Decimal(1540), billed_kg=Decimal(1540), freight=Decimal(total),
                      total=Decimal(total), currency="EUR", total_eur=Decimal(total), transit_days=days)  # fmt: skip


def gateway(monkeypatch, answer) -> tuple[list[dict], list[dict]]:
    asked, posted = [], []

    async def ask_json(ctx, **kw):
        asked.append(kw)
        return LlmResult(data=await answer(), model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    async def post(ctx, issues):
        posted.extend(issues)

    monkeypatch.setattr(llm, "ask_json", ask_json)
    monkeypatch.setattr(feedback, "post", post)
    return asked, posted


async def test_a_request_the_rules_read_whole_takes_no_call(conn, monkeypatch):
    asked, posted = gateway(monkeypatch, None)
    ctx = Context(replace(settings(), alert_url="http://alert"), conn, None, None, None, None)
    text, subject, rfq = request("rfq-port-to-port.json")
    assert await novelty.check(ctx, rfq, text, subject) == rfq
    assert (asked, posted, novelty.mapped(ctx, 1)) == ([], [], {})


async def test_an_unseen_requirement_moves_the_selection_names_the_papers_and_alerts_at_once(conn, monkeypatch):
    async def answer():
        return {"exclude_airlines": [], "only_airlines": ["Kestrel Air Cargo", "Ghost Air"], "arrive_by": "2026-12-01",
                "documents": ["customs documents"], "conditions": ["goods value EUR 5000"]}  # fmt: skip

    asked, posted = gateway(monkeypatch, answer)
    ctx = Context(replace(settings(), alert_url="http://alert"), conn, None, None, None, None)
    store.save_carriers(ctx, [{"name": "Kestrel Air Cargo", "kind": "airline", "email": "k@x", "serves": "JFK"}])
    text, subject, rfq = request("synthetic-novel-requirement.json")
    await novelty.check(ctx, rfq, text, subject)
    await asyncio.gather(*ctx.tasks)
    assert len(asked) == 1 and "Kestrel Air Cargo" in asked[0]["user"] and "1540" not in asked[0]["user"]
    assert posted[0]["issue"] == "novel_requirement" and "customs documents" in posted[0]["leftover"]
    required = novelty.mapped(ctx, 1)
    assert required["only_airlines"] == ["Kestrel Air Cargo"]  # a name the mail does not hold is dropped
    assert required["conditions"] == []  # an amount with a currency never enters the quote's text
    options = [option("Cheap Air", 900, 2), option("Kestrel Air Cargo", 1200, 9), option("Kestrel Air Cargo", 1500, 3)]
    picked = selection.select(rfq, selection.required(options, required["only_airlines"], datetime(2026, 12, 1).date(),
                                                      READY))  # fmt: skip
    assert picked.total == 1500  # Kestrel, and the one that is at JFK by 1 December
    body = novelty.with_note("Hello,\n\nOffer.\n\nBook it.\n\nKind regards\nLena\nAir Export, PIXEL Air Freight",
                             required, "en")  # fmt: skip
    assert "Offer.\n\nDocuments we will need: customs documents.\n\nBook it." in body


async def test_a_hanging_gateway_never_holds_the_quote_and_still_alerts(conn, monkeypatch):
    async def answer():
        await asyncio.sleep(60)

    _, posted = gateway(monkeypatch, answer)
    monkeypatch.setattr(novelty, "WAIT_S", 0.1)
    ctx = Context(replace(settings(), alert_url="http://alert"), conn, None, None, None, None)
    text, subject, rfq = request("synthetic-novel-requirement.json")
    assert await asyncio.wait_for(novelty.check(ctx, rfq, text, subject), 1) == rfq
    await asyncio.gather(*ctx.tasks)
    assert posted[0]["mapped"] == {"error": "TimeoutError"} and novelty.mapped(ctx, 1) == {}


def test_a_dangerous_goods_line_with_n_o_s_is_read_whole():
    """Sessions 33097 and 38213: "n.o.s." split the packing list's DG line, and "(contains phosphoric acid)" cost a
    model call and a note in the quote though the DG rule had read the line."""
    line = "Dangerous goods: UN1760, Class 8, Corrosive liquid, n.o.s. (contains phosphoric acid)"
    assert novelty.leftover(line) == []
