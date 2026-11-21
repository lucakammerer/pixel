"""Tariffs, carriers and pickup zones as rows. Every tariff row carries its version and validity."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

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
    now = datetime.now(UTC).isoformat()
    ctx.db.executemany(
        "INSERT OR REPLACE INTO tariffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                t.carrier,
                t.origin,
                t.destination,
                t.version,
                t.valid_from.isoformat(),
                t.valid_to.isoformat(),
                t.currency,
                t.transit_days,
                str(t.minimum),
                json.dumps([[str(k), str(r)] for k, r in t.breaks]),
                str(t.fuel_per_kg),
                str(t.security_per_kg),
                t.source,
                now,
            )
            for t in tariffs
        ],
    )


def _row(row) -> LaneTariff:
    return LaneTariff(
        carrier=row["carrier"],
        origin=row["origin"],
        destination=row["destination"],
        currency=row["currency"],
        valid_from=date.fromisoformat(row["valid_from"]),
        valid_to=date.fromisoformat(row["valid_to"]),
        version=row["version"],
        transit_days=row["transit_days"],
        minimum=Decimal(row["minimum"]),
        breaks=tuple((Decimal(k), Decimal(r)) for k, r in json.loads(row["breaks"])),
        fuel_per_kg=Decimal(row["fuel_per_kg"]),
        security_per_kg=Decimal(row["security_per_kg"]),
        source=row["source"],
    )


def lookup(ctx: Context, origin: str, destination: str, on: date) -> list[LaneTariff]:
    """Per carrier the newest version valid on that day."""
    rows = ctx.db.execute(
        "SELECT * FROM tariffs WHERE origin = ? AND destination = ? AND valid_from <= ? AND valid_to >= ? "
        "ORDER BY valid_from DESC, ingested_at DESC",
        (origin, destination, on.isoformat(), on.isoformat()),
    ).fetchall()
    newest: dict[str, LaneTariff] = {}
    for row in rows:
        newest.setdefault(row["carrier"], _row(row))
    return list(newest.values())


def has_source(ctx: Context, source: str) -> bool:
    return ctx.db.execute("SELECT 1 FROM tariffs WHERE source = ? LIMIT 1", (source,)).fetchone() is not None


def save_carriers(ctx: Context, rows: list[dict[str, str]]) -> None:
    ctx.db.executemany(
        "INSERT OR REPLACE INTO carriers VALUES (?, ?, ?, ?)",
        [(r["name"].strip(), r["kind"].strip(), r["email"].strip(), r["serves"].strip()) for r in rows],
    )


def save_zones(ctx: Context, rows: list[dict[str, str]]) -> None:
    ctx.db.executemany(
        "INSERT OR REPLACE INTO pickup_zones VALUES (?, ?, ?)",
        [(r["zone"].strip(), r["area"].strip(), r["airport"].strip()) for r in rows],
    )


def carriers(ctx: Context, kind: str, serves: str) -> list[tuple[str, str]]:
    """(name, email) of airlines serving a destination or truckers serving a zone."""
    rows = ctx.db.execute("SELECT name, email, serves FROM carriers WHERE kind = ? ORDER BY name", (kind,))
    return [(r["name"], r["email"]) for r in rows if serves in r["serves"].split()]


def carrier_by_email(ctx: Context, email: str) -> tuple[str, str] | None:
    """(name, kind) of the carrier writing from this address."""
    row = ctx.db.execute("SELECT name, kind FROM carriers WHERE lower(email) = lower(?)", (email,)).fetchone()
    return (row["name"], row["kind"]) if row else None


def airport_of(ctx: Context, zone: str) -> str | None:
    row = ctx.db.execute("SELECT airport FROM pickup_zones WHERE zone = ?", (zone,)).fetchone()
    return row["airport"] if row else None


def zone_airports(ctx: Context) -> set[str]:
    return {row["airport"] for row in ctx.db.execute("SELECT DISTINCT airport FROM pickup_zones")}
