from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pixel.agents.rfq import agent
from pixel.config import settings
from pixel.contracts import RateOption, Rfq, Source
from pixel.core import events
from pixel.core.context import Context


@pytest.mark.parametrize(
    ("source", "requested", "fx_location", "expected_currency", "expected_total"),
    [
        ("USD", "EUR", None, "EUR", "80.00"),
        ("EUR", "USD", "session", "USD", "125.00"),
        ("EUR", "USD", "history", "USD", "125.00"),
        ("EUR", "USD", None, "EUR", "100"),
    ],
)
def test_quote_currency(conn, source, requested, fx_location, expected_currency, expected_total):
    ctx = Context(settings(), conn, None, None, None, None)
    now = datetime(2026, 11, 21, tzinfo=UTC)
    rfq = Rfq(
        session_id=7, message_id=7, customer="buyer@example.com", origin="AAA", destination="BBB",
        weight_kg=Decimal(10), received_at=now, deadline_at=now, currency=requested,
    )  # fmt: skip
    usd_fx = Decimal("0.8")
    option = RateOption(
        source=Source.PORTAL, carrier="Test Air", origin=rfq.origin, destination=rfq.destination,
        chargeable_kg=rfq.weight_kg, billed_kg=rfq.weight_kg, freight=Decimal(100), total=Decimal(100),
        currency=source, fx_eur=usd_fx if source == "USD" else None,
        total_eur=Decimal(80 if source == "USD" else 100), transit_days=2,
    )  # fmt: skip
    reference = option.model_copy(
        update={"carrier": "Other Air", "currency": "USD", "fx_eur": usd_fx,
                "freight": Decimal(200), "total": Decimal(200), "total_eur": Decimal(160)},
    )  # fmt: skip
    options = [option]
    if fx_location == "session":
        options.append(reference)
    if fx_location == "history":
        for rate in (Decimal("0.5"), usd_fx):
            historical = reference.model_copy(update={"fx_eur": rate, "total_eur": reference.total * rate})
            events.emit(ctx, "option.found", "rfq", session_id=6, payload={"option": historical.model_dump(mode="json")})

    quote = agent._quote(ctx, rfq, options, None, fallback=False)

    assert (quote.currency, quote.total) == (expected_currency, Decimal(expected_total))
    assert (quote.option.currency, quote.option.total) == (quote.currency, quote.total)
    assert (quote.option.carrier, quote.option.total_eur) == (option.carrier, option.total_eur)
