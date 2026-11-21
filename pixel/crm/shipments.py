import secrets
from datetime import UTC, datetime

from pixel.contracts import Quote, Shipment
from pixel.core.context import Context


def create(ctx: Context, quote: Quote) -> Shipment:
    """A new shipment with a fresh id; that id is the booking's `team_reference`."""
    shipment = Shipment(
        id=f"PX-{secrets.token_hex(3).upper()}",
        session_id=quote.session_id,
        booking_id="",
        customer=quote.rfq.customer,
        origin=quote.option.origin,
        destination=quote.option.destination,
        weight_kg=quote.rfq.weight_kg,
        carrier=quote.option.carrier,
        status="booking",
        transit_days=quote.option.transit_days,
        updated_at=datetime.now(UTC),
    )
    save(ctx, shipment)
    return shipment


def save(ctx: Context, shipment: Shipment) -> None:
    ctx.db.execute(
        "INSERT OR REPLACE INTO shipments (id, session_id, booking_id, customer, origin, destination,"
        " booking_reference, status, updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            shipment.id, shipment.session_id, shipment.booking_id, shipment.customer.lower(), shipment.origin,
            shipment.destination, shipment.booking_reference, shipment.status, shipment.updated_at.isoformat(),
            shipment.model_dump_json(),
        ),
    )  # fmt: skip


def get(ctx: Context, shipment_id: str) -> Shipment | None:
    found = _select(ctx, "UPPER(id) = UPPER(?)", [shipment_id])
    return found[0] if found else None


def for_session(ctx: Context, session_id: int) -> Shipment | None:
    found = _select(ctx, "session_id = ?", [session_id])
    return found[0] if found else None


def recent(ctx: Context, limit: int = 50) -> list[Shipment]:
    return _select(ctx, "1 = 1", [], limit)


def find(
    ctx: Context,
    *,
    reference: str | None = None,
    customer: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
) -> list[Shipment]:
    """What the tracking agent searches with: any id we or the airline gave, else customer and lane."""
    if reference:
        return _select(ctx, "UPPER(id) = UPPER(?) OR UPPER(booking_reference) = UPPER(?)", [reference, reference])
    where, args = [], []
    for column, value in (
        ("customer", customer and customer.lower()),
        ("origin", origin),
        ("destination", destination),
    ):
        if value:
            where.append(f"{column} = ?")
            args.append(value.upper() if column != "customer" else value)
    return _select(ctx, " AND ".join(where), args) if where else []


def _select(ctx: Context, where: str, args: list, limit: int = 1000) -> list[Shipment]:
    rows = ctx.db.execute(
        f"SELECT data FROM shipments WHERE {where} ORDER BY updated_at DESC LIMIT ?", [*args, limit]
    ).fetchall()
    return [Shipment.model_validate_json(r["data"]) for r in rows]
