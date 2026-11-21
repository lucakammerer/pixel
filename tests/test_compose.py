from datetime import UTC, datetime
from decimal import Decimal

from pixel import llm
from pixel.agents.rfq import compose
from pixel.contracts import LlmResult, Message, RateOption, Rfq, Source
from pixel.core import events

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


def rfq(language: str) -> Rfq:
    return Rfq(
        session_id=1, message_id=1, customer="buyer@example.com", origin="AAA", destination="BBB",
        weight_kg=Decimal(1200), language=language, received_at=NOW, deadline_at=NOW,
    )  # fmt: skip


OPTION = RateOption(
    source=Source.SHEET, carrier="Example Air", origin="AAA", destination="BBB", chargeable_kg=Decimal(1200),
    billed_kg=Decimal(1200), freight=Decimal("1000.00"), fuel=Decimal("200.00"), security=Decimal("34.565"),
    total=Decimal("1234.565"), currency="EUR", total_eur=Decimal("1234.565"), transit_days=3,
)  # fmt: skip


def test_total_is_written_once_without_a_thousands_separator_in_every_language():
    _, en = compose.render(rfq("en"), OPTION)
    _, de = compose.render(rfq("de"), OPTION)
    assert en.count("1234.57 EUR") == 1 and de.count("1234.57 EUR") == 1
    assert "1,234" not in en and "1.234" not in de
    assert "Example Air" in en and "3 days" in en and "3 Tage" in de


def test_pickup_is_named():
    option = OPTION.model_copy(update={"trucker": "Example Trucks", "pickup": Decimal(50)})
    _, body = compose.render(rfq("en"), option)
    assert "Example Trucks" in body


async def test_opening_with_digits_is_rejected(monkeypatch):
    async def fake_ask(ctx, **kw):
        return LlmResult(data={"sentence": "Your 1200 kg are welcome."}, model="m", tokens_in=1, tokens_out=1,
                         cost_usd=Decimal(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    message = Message(id=1, session_id=1, direction="in", sender="b@example.com", to="u@example.com", subject="RFQ",
                      body="hi", sent_at=NOW)  # fmt: skip
    assert await compose.opening(None, rfq("en"), message) is None


async def test_opening_failure_never_blocks(monkeypatch):
    async def fail(ctx, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", fail)
    monkeypatch.setattr(events, "emit", lambda *a, **kw: None)
    message = Message(id=1, session_id=1, direction="in", sender="b@example.com", to="u@example.com", subject="RFQ",
                      body="hi", sent_at=NOW)  # fmt: skip
    assert await compose.opening(None, rfq("en"), message) is None
