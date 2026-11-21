"""Tariffs, carriers and pickup zones as rows. Every tariff row carries its version and validity."""

from datetime import date

from pixel.contracts import LaneTariff
from pixel.core.context import Context

SCHEMA = """
CREATE TABLE IF NOT EXISTS tariffs (
    carrier TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, version TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, currency TEXT NOT NULL, transit_days INTEGER NOT NULL,
    minimum TEXT NOT NULL, breaks TEXT NOT NULL, fuel_per_kg TEXT NOT NULL, security_per_kg TEXT NOT NULL,
    source TEXT NOT NULL, ingested_at TEXT NOT NULL,
    PRIMARY KEY (carrier, origin, destination, version)
);
CREATE TABLE IF NOT EXISTS carriers (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, serves TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pickup_zones (zone TEXT PRIMARY KEY, area TEXT NOT NULL, airport TEXT NOT NULL);
"""


def upsert(ctx: Context, tariffs: list[LaneTariff]) -> None:
    raise NotImplementedError


def lookup(ctx: Context, origin: str, destination: str, on: date) -> list[LaneTariff]:
    """Per carrier the newest version valid on that day."""
    raise NotImplementedError


def carriers(ctx: Context, kind: str, serves: str) -> list[tuple[str, str]]:
    """(name, email) of airlines serving a destination or truckers serving a zone."""
    raise NotImplementedError


def airport_of(ctx: Context, zone: str) -> str | None:
    raise NotImplementedError
