from pixel.contracts import Booking
from pixel.core.context import Context


def save(ctx: Context, booking: Booking) -> None:
    ctx.db.execute(
        "INSERT OR REPLACE INTO bookings (id, quote_id, session_id, team_reference, booking_reference, status,"
        " created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            booking.id, booking.quote_id, booking.session_id, booking.team_reference, booking.booking_reference,
            booking.status, booking.created_at.isoformat(), booking.model_dump_json(),
        ),
    )  # fmt: skip


def get(ctx: Context, booking_id: str) -> Booking | None:
    return _one(ctx, "id = ?", booking_id)


def by_reference(ctx: Context, booking_reference: str) -> Booking | None:
    return _one(ctx, "UPPER(booking_reference) = UPPER(?)", booking_reference)


def for_session(ctx: Context, session_id: int) -> Booking | None:
    """The session's newest booking."""
    return _one(ctx, "session_id = ?", session_id)


def _one(ctx: Context, where: str, value) -> Booking | None:
    row = ctx.db.execute(
        f"SELECT data FROM bookings WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT 1", (value,)
    ).fetchone()
    return Booking.model_validate_json(row["data"]) if row else None


def all_by_id(ctx: Context) -> dict[str, Booking]:
    """Every booking by its id, for joining onto shipments."""
    return {r["id"]: Booking.model_validate_json(r["data"]) for r in ctx.db.execute("SELECT id, data FROM bookings")}
