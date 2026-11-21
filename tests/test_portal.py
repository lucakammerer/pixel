from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from pixel.api import portal as portal_module
from pixel.api.portal import NotOnPortal, Portal, RateLimited
from pixel.contracts import Source

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
