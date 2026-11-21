import importlib
import pkgutil
from datetime import UTC, datetime
from decimal import Decimal

import pixel
from pixel.config import settings
from pixel.contracts import RateOption, Source
from pixel.core.orchestrator import deadline_for


def test_every_module_imports():
    for module in pkgutil.walk_packages(pixel.__path__, "pixel."):
        importlib.import_module(module.name)


def test_schema_creates(conn):
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "sessions", "events", "quotes", "bookings", "shipments", "tariffs"} <= tables


def test_money_survives_json():
    option = RateOption(
        source=Source.SHEET, carrier="x", origin="FRA", destination="JFK", chargeable_kg=Decimal(167),
        billed_kg=Decimal(167), freight=Decimal("370.74"), total=Decimal("370.74"), currency="EUR",
        total_eur=Decimal("370.74"), transit_days=4,
    )  # fmt: skip
    assert RateOption.model_validate_json(option.model_dump_json()) == option


def test_deadline_order():
    d = deadline_for(datetime(2026, 11, 21, 16, 0, tzinfo=UTC), settings())
    assert d.received_at < d.target_at < d.hard_at < d.expires_at
