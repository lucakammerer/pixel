from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from pixel.api import portal as portal_module
from pixel.api.portal import NotOnPortal, Portal, RateLimited
from pixel.config import settings
from pixel.contracts import Rfq, Source
from pixel.core import events
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.pricing import portal_rates

BASE = "https://portal.test"
RATE = {
    "rate_id": "R-abcdefgh12",
    "airline": "Some Air",
    "airline_code": "SA",
    "origin": "AAA",
    "destination": "BBB",
    "weight_kg": "100",
    "chargeable_weight_kg": "167.00",
    "total": "200.00",
    "currency": "USD",
    "fx_eur_per_usd": "0.9150",
    "transit_days": 3,
    "valid_until": "2026-11-21T16:15:00Z",
    "dataset_version": "v7",
}
LIMITS = {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "4"}


@pytest.fixture
async def portal():
    async with httpx.AsyncClient(base_url=BASE) as client:
        yield Portal(client)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(portal_module.asyncio, "sleep", fake_sleep)
    return slept


async def read(portal, **kw):
    return await portal.rate("AAA", "BBB", "SA", Decimal(100), Decimal(1), **kw)


@respx.mock
async def test_rate_parses_and_reads_limit(portal):
    respx.get(f"{BASE}/v1/portal/rates").respond(200, json=RATE, headers=LIMITS)
    option = await read(portal)
    assert (portal.rate_limit, portal.rate_remaining) == (10, 4)
    assert option.source == Source.PORTAL and option.rate_id == "R-abcdefgh12"
    assert option.total == Decimal("200.00") and option.fx_eur == Decimal("0.9150")
    assert option.total_eur == Decimal("183.00")
    assert option.chargeable_kg == option.billed_kg == Decimal("167.00")
    assert option.valid_until == datetime(2026, 11, 21, 16, 15, tzinfo=UTC)


@respx.mock
async def test_429_waited_out_when_deadline_allows(portal, no_sleep):
    respx.get(f"{BASE}/v1/portal/rates").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=RATE)]
    )
    option = await read(portal, not_after=datetime.now(UTC) + timedelta(minutes=2))
    assert option.rate_id == RATE["rate_id"] and no_sleep == [7.0]


@respx.mock
async def test_429_raises_when_deadline_too_close(portal, no_sleep):
    route = respx.get(f"{BASE}/v1/portal/rates").respond(429, headers={"Retry-After": "30"})
    with pytest.raises(RateLimited):
        await read(portal, not_after=datetime.now(UTC) + timedelta(seconds=10))
    with pytest.raises(RateLimited):
        await read(portal, not_after=datetime.now(UTC) + timedelta(seconds=10))
    assert route.call_count == 1 and no_sleep == []


@respx.mock
@pytest.mark.parametrize("status", [404, 503])
async def test_not_on_portal_carries_hint(portal, status):
    respx.get(f"{BASE}/v1/portal/rates").respond(status, json={"detail": "mail some-air@example.test"})
    with pytest.raises(NotOnPortal) as err:
        await read(portal)
    assert "some-air@example.test" in err.value.hint


@respx.mock
async def test_brackets_404_is_not_on_portal(portal):
    respx.get(f"{BASE}/v1/portal/brackets").respond(404, json={"detail": "write to x@example.test"})
    with pytest.raises(NotOnPortal):
        await portal.brackets("Nobody Air")


class LanePortal:
    """One airline on one lane; the weight we ask for decides the total."""

    rate_remaining = None

    def __init__(self, totals: dict[int, str]):
        self.totals = totals

    async def lanes(self):
        return [portal_module.Lane("AAA", "BBB", (("SA", "Some Air"),))]

    async def brackets(self, airline):
        return (0, 100, 500)

    async def rate(self, origin, destination, airline, weight_kg, volume_cbm, *, not_after=None):
        if int(weight_kg) not in self.totals:
            raise RateLimited(60)
        rate = {**RATE, "currency": "EUR", "chargeable_weight_kg": str(weight_kg), "total": self.totals[int(weight_kg)]}
        return portal_module._option({**rate, "rate_id": f"R-{weight_kg}"})


@pytest.mark.parametrize(
    ("totals", "total", "billed", "rate_id"),
    [
        ({400: "1000.00", 500: "950.00"}, "950.00", 500, "R-500"),  # the next break is cheaper: bill it
        ({400: "1000.00", 500: "1100.00"}, "1000.00", 400, "R-400"),
        ({400: "1000.00"}, "1000.00", 400, "R-400"),  # the second read fails: keep what we have
    ],
)
async def test_the_next_break_is_billed_when_it_is_cheaper(conn, monkeypatch, totals, total, billed, rate_id):
    monkeypatch.setattr(portal_rates, "_lanes", None)
    monkeypatch.setattr(portal_rates, "_breaks", {})
    monkeypatch.setattr(events, "emit", lambda *a, **kw: None)
    now = datetime.now(UTC)
    ctx = Context(settings(), conn, None, LanePortal(totals), None, None)
    rfq = Rfq(session_id=1, message_id=1, customer="c@x", origin="AAA", destination="BBB", weight_kg=Decimal(400),
              received_at=now, deadline_at=now)  # fmt: skip
    deadline = Deadline(received_at=now, target_at=now, hard_at=now, expires_at=now)
    (option,) = await portal_rates.options(ctx, rfq, deadline)
    assert (option.total, option.billed_kg, option.chargeable_kg, option.rate_id) == (
        Decimal(total), Decimal(billed), Decimal(400), rate_id,
    )  # fmt: skip
