"""Rounds 27 to 29: the portal's rate API answered 503 "Rates are still published on the portal page". Offline: the
portal is mocked at HTTP level, carriers and the rate desk answer through `bids.record`."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
import respx

from pixel import llm
from pixel.api.mailbox import RATEDESK
from pixel.api.portal import Portal
from pixel.contracts import LaneTariff, Preference, Source
from pixel.core import db, events, timing
from pixel.pricing import bids, engine, portal_rates, rate_card, selection, store
from tests.test_bids import ctx, deadline, kinds, mail, rfq  # noqa: F401  ctx is the fixture

BASE = "https://portal.test"
PORTS = {
    "lanes": [
        {"origin": "AAA", "destination": "BBB", "airlines": [{"code": "A1", "name": "Air One"},
                                                             {"code": "A2", "name": "Air Two"}]}
    ]
}  # fmt: skip
RATE = {"rate_id": "R-portal", "origin": "AAA", "destination": "BBB", "weight_kg": "100", "currency": "EUR",
        "chargeable_weight_kg": "100", "total": "650.00", "transit_days": 3, "valid_until": "2026-11-21T18:15:00Z",
        "dataset_version": "v1"}  # fmt: skip
DESK = (
    'Hello,\n\nportal rates for AAA-BBB, 100.0 kg, 0.0 cbm (your mail "Rate request AAA-BBB, 100 kg"):\n\n'
    "- Air One (A1): 690.00 EUR, 3 days, rate_id R-desk-one, valid until 21 Nov 2026 18:38 UTC\n"
    "- Air Two (A2): 600.00 EUR, 4 days, rate_id R-desk-two, valid until 21 Nov 2026 18:38 UTC\n\n"
    "Book with the rate_id on the portal or by mail to the airline.\n\nBest regards\nRate Desk"
)


@pytest.fixture(autouse=True)
def offline(ctx, monkeypatch):  # noqa: F811
    async def no_model(*args, **kw):
        raise AssertionError("no model call on this path")

    monkeypatch.setattr(llm, "ask_json", no_model)  # bids.record catches it as an `error` event
    ctx.settings = replace(ctx.settings, model_check="")  # no second reader on this path
    monkeypatch.setattr(engine, "PORTAL_HEAD_START_S", 0.05)
    with respx.mock(base_url=BASE, assert_all_called=False) as portal:
        ctx.portal = Portal(httpx.AsyncClient(base_url=BASE))
        portal.get("/v1/portal/brackets").respond(200, json={"breaks_kg": [0]})
        yield portal


def answer_rates(portal, case: str) -> None:
    portal.get("/v1/portal/ports").respond(200, json={"lanes": []} if case == "no lane" else PORTS)
    route = portal.get("/v1/portal/rates")
    if case == "healthy":
        names = {"A1": "Air One", "A2": "Air Two"}
        route.mock(
            side_effect=lambda r: httpx.Response(
                200, json={**RATE, "airline_code": r.url.params["airline"], "airline": names[r.url.params["airline"]]}
            )
        )
    elif case == "404":
        route.respond(404, json={"detail": "This airline is not on the portal. Email the airline for a rate."})
    elif case == "429":
        route.respond(429, headers={"Retry-After": "60"})
    elif case.isdigit():
        route.respond(int(case), json={"detail": "The rate API is unavailable."})


def desk_asked(ctx) -> list[dict]:  # noqa: F811
    return [r for r in kinds(ctx, "bid.requested") if r["email"] == RATEDESK]


async def wait_for(ctx, n: int) -> None:  # noqa: F811
    for _ in range(50):
        if len(kinds(ctx, "bid.requested")) >= n:
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "case, airlines_mailed, desk",
    [
        ("healthy", 0, 0),
        ("no lane", 2, 0),
        ("404", 2, 0),
        ("429", 2, 0),
        ("503", 2, 1),
        ("502", 2, 1),  # Cloudflare's own failures
        ("520", 2, 1),
        ("524", 2, 1),
        ("403", 2, 1),  # a bot check in front of the API, not only in front of the page
    ],
)
async def test_only_a_rate_api_outage_asks_the_rate_desk(ctx, offline, case, airlines_mailed, desk):  # noqa: F811
    answer_rates(offline, case)
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    found = await engine.options(ctx, request, deadline(0.3))
    asked = kinds(ctx, "bid.requested")
    assert (len(asked) - len(desk_asked(ctx)), len(desk_asked(ctx))) == (airlines_mailed, desk)
    assert {o.source for o in found} == ({Source.PORTAL} if case == "healthy" else set())


async def test_a_rate_read_that_fails_after_the_head_start_still_asks_the_desk(ctx, offline, monkeypatch):  # noqa: F811
    """The desk decision used to be one sample of the event log after a one-second head start: a read that answered
    503 at 1.2 s, or a hung one that timed out, was missed and the desk was never asked (rounds 120, 123)."""
    offline.get("/v1/portal/ports").respond(200, json=PORTS)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(503, json={"detail": "The rate API is unavailable."})

    offline.get("/v1/portal/rates").mock(side_effect=slow)
    monkeypatch.setattr(engine, "PORTAL_HEAD_START_S", 0.02)
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    await engine.options(ctx, request, deadline(0.6))
    assert len(desk_asked(ctx)) == 1


@pytest.mark.parametrize("door", [False, True])
async def test_a_cheaper_desk_rate_is_quoted_with_its_rate_id_and_the_pickup(ctx, offline, door):  # noqa: F811
    answer_rates(offline, "503")
    request = rfq(preference=Preference.CHEAPEST, door_pickup=door,
                  **({"pickup_postcode": "12345", "pickup_zone": "12"} if door else {}))  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))

    async def answer():
        # Airlines, truckers and the desk are asked together; Air Two never answers its own mail.
        await wait_for(ctx, 5 if door else 3)
        await bids.record(ctx, mail("one@air", "All-in EUR 700.00, transit 3 days"))
        await bids.record(ctx, mail(RATEDESK, DESK))
        if door:
            await bids.record(ctx, mail("a@truck", "Pickup EUR 90.00"))
            await bids.record(ctx, mail("b@truck", "We can do it for 75,00 EUR"))

    found, _ = await asyncio.gather(engine.options(ctx, request, deadline(1)), answer())
    best = selection.select(request, found)
    pickup = D("75.00") if door else D(0)
    assert (best.source, best.carrier, best.carrier_code, best.rate_id) == (Source.RATEDESK, "Air Two", "A2",
                                                                           "R-desk-two")  # fmt: skip
    assert best.total_eur == D("600.00") + pickup
    assert len(desk_asked(ctx)) == 1


@pytest.mark.parametrize("desk", ["silent", "late"])
async def test_a_silent_or_late_desk_leaves_the_airline_bids_and_the_deadline(ctx, offline, desk):  # noqa: F811
    answer_rates(offline, "503")
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))

    async def answer():
        await wait_for(ctx, 2)
        await bids.record(ctx, mail("one@air", "All-in EUR 700.00, transit 3 days"))
        await bids.record(ctx, mail("two@air", "All-in EUR 650.00, transit 4 days"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    found, _ = await asyncio.gather(engine.options(ctx, request, deadline(0.4)), answer())
    assert loop.time() - started < 0.6  # the target, never the hard deadline: that is the orchestrator's
    assert {(o.carrier, o.total) for o in found} == {("Air One", D("700.00")), ("Air Two", D("650.00"))}
    if desk == "late":
        await bids.record(ctx, mail(RATEDESK, DESK))  # stored for the fallback, read without a model
        assert kinds(ctx, "bid.received")[-1]["priced"] is True
    assert not kinds(ctx, "error")


async def test_a_desk_answer_without_rates_is_not_read_by_the_model(ctx):  # noqa: F811
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=rfq().model_dump(mode="json"))
    await bids.record(ctx, mail(RATEDESK, "Hello,\n\nrates for AAA-BBB are currently unavailable.\n\nRate Desk"))
    assert kinds(ctx, "bid.received")[-1]["priced"] is False and not kinds(ctx, "error")


@pytest.mark.parametrize("desk_replies", [True, False])
async def test_sheet_quote_gives_pending_desk_a_short_extra_window(ctx, offline, monkeypatch, desk_replies):  # noqa: F811
    answer_rates(offline, "503")
    monkeypatch.setattr(bids, "DESK_CUTOFF_S", 0.6)
    monkeypatch.setattr(bids, "POLL_S", 0.01)
    ctx.settings = replace(ctx.settings, bid_wait_s=0)
    request = rfq(preference=Preference.FASTEST)
    today = request.received_at.date()
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin=request.origin, destination=request.destination, currency="EUR",
        valid_from=today, valid_to=today, version="test", transit_days=5, minimum=D(400),
        breaks=((D(0), D(4)),), fuel_per_kg=D(0), security_per_kg=D(0), source="test",
    )])  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    started = timing.now()
    window = replace(deadline(2), received_at=started, target_at=started + timedelta(seconds=0.15))

    async def answer():
        await wait_for(ctx, 2)
        await asyncio.sleep(max(0, (window.target_at - timing.now()).total_seconds()) + 0.04)
        if desk_replies:
            await bids.record(ctx, mail(RATEDESK, DESK))

    found, _ = await asyncio.gather(engine.options(ctx, request, window), answer())
    best = selection.select(request, found)
    assert best.source == (Source.RATEDESK if desk_replies else Source.SHEET)
    assert best.total == (D("690.00") if desk_replies else D("400.00"))
    if not desk_replies:
        assert timing.now() >= started + timedelta(seconds=0.6)


async def test_a_lane_nothing_else_prices_asks_the_desk_and_mails_the_airlines_it_names(ctx, offline):  # noqa: F811
    """R1: no sheet, no portal lane, no airline on the carrier list for the destination left the request no price."""
    answer_rates(offline, "no lane")
    request = rfq().model_copy(update={"destination": "ZZZ"})
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    await engine.options(ctx, request, deadline(0.3))
    assert [r["email"] for r in kinds(ctx, "bid.requested")] == [RATEDESK]
    desk = ("Hello,\n\nportal rates for AAA-ZZZ:\n\n- Air Two (A2): not on the portal, email two@air\n\n"
            "Book with the rate_id on the portal or by mail to the airline.\n\nBest regards\nRate Desk")  # fmt: skip
    await bids.record(ctx, mail(RATEDESK, desk))
    await bids.record(ctx, mail(RATEDESK, desk))
    assert [r["email"] for r in kinds(ctx, "bid.requested")] == [RATEDESK, "two@air"]


async def test_only_the_requested_airline_gets_a_bid_enquiry(ctx, offline):  # noqa: F811
    answer_rates(offline, "no lane")
    request = rfq(required_carriers=("Air Two",))
    await engine.options(ctx, request, deadline(0.1))
    assert [bid["email"] for bid in kinds(ctx, "bid.requested")] == ["two@air"]


async def test_a_rate_that_answered_503_is_read_again_before_the_quote(ctx, offline, monkeypatch):  # noqa: F811
    """Rounds 120 and 121: the rate API answered 503 at the first read and the sheet airline was quoted; the first
    outage ended mid-round. A second read inside the desk wait prices the faster airline."""
    offline.get("/v1/portal/ports").respond(200, json=PORTS)
    loop = asyncio.get_running_loop()
    recovers_at = loop.time() + 0.15  # the outage ends here, between the first read and the first probe
    reads: dict[str, int] = {}

    def rate(request: httpx.Request) -> httpx.Response:
        code = request.url.params["airline"]
        reads[code] = reads.get(code, 0) + 1
        if loop.time() < recovers_at:
            return httpx.Response(503, json={"detail": "The rate API is unavailable."})
        names = {"A1": "Air One", "A2": "Air Two"}
        return httpx.Response(200, json={**RATE, "airline_code": code, "airline": names[code]})

    offline.get("/v1/portal/rates").mock(side_effect=rate)
    monkeypatch.setattr(bids, "DESK_CUTOFF_S", 0.6)
    monkeypatch.setattr(bids, "POLL_S", 0.01)
    monkeypatch.setattr(portal_rates, "PROBE_AT_S", (0.3,))
    ctx.settings = replace(ctx.settings, bid_wait_s=0)
    request = rfq(preference=Preference.FASTEST)
    today = request.received_at.date()
    store.upsert(ctx, [LaneTariff(
        carrier="Air Three", origin=request.origin, destination=request.destination, currency="EUR",
        valid_from=today, valid_to=today, version="test", transit_days=5, minimum=D(400),
        breaks=((D(0), D(4)),), fuel_per_kg=D(0), security_per_kg=D(0), source="test",
    )])  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    started = timing.now()
    window = replace(deadline(2), received_at=started, target_at=started + timedelta(seconds=0.15))
    found = await engine.options(ctx, request, window)
    best = selection.select(request, found)
    assert (best.source, best.rate_id, best.transit_days) == (Source.PORTAL, "R-portal", 3)
    assert reads == {"A1": 2, "A2": 2}
    assert timing.now() < started + timedelta(seconds=1.5)


def test_a_price_without_a_rate_id_keeps_the_airline_probed(ctx):  # noqa: F811
    """A rate card price (PR #410) has no rate id: only the API's own answer stops the probes for that airline."""
    events.emit(ctx, "option.failed", "pricing", session_id=1,
                payload={"source": "portal", "carrier": "Air One", "error": "RatesUnavailable"})  # fmt: skip
    card = {"source": "portal", "carrier": "Air One", "rate_id": None}
    events.emit(ctx, "option.found", "pricing", session_id=1, payload={"option": card})
    assert portal_rates._unpriced(ctx, rfq()) == ({"Air One"}, False)
    events.emit(ctx, "option.found", "pricing", session_id=1, payload={"option": card | {"rate_id": "R-1"}})
    assert portal_rates._unpriced(ctx, rfq()) == (set(), False)


async def test_a_503_prices_the_airline_from_the_rate_card_at_the_current_versions_factor(ctx, offline):  # noqa: F811
    """Round 94: the API, its page and the desk were silent; Xenon's 3399.00 for 1133 kg, read in v1, prices 1135.6 kg.
    A later version raised every rate by 15 % (v1+ef9a74): one bracket read in both gives the factor for all."""
    answer_rates(offline, "503")
    db.put(ctx.db, "rate_card.breaks.A1", '["0", "1000"]')

    def read(version: str, kg: str, total: str) -> None:
        events.emit(ctx, rate_card.KIND, "pricing", payload={
            "origin": "AAA", "destination": "BBB", "code": "A1", "carrier": "Air One", "kg": kg, "total": total,
            "currency": "EUR", "fx_eur": None, "transit_days": 2, "version": version})  # fmt: skip

    read("v1", "1133", "3399.00")
    read("v1", "500", "2000.00")
    read("v1+ef9a74", "500", "2300.00")
    found = await portal_rates.options(ctx, rfq().model_copy(update={"weight_kg": D("1135.6")}), deadline())
    assert [(o.carrier, o.source, o.total_eur, o.rate_id) for o in found] == [
        ("Air One", Source.PORTAL, D("3917.82"), None)
    ]


@pytest.mark.parametrize("failure", ["connection reset", "lane list", "rate limit"])
async def test_any_outage_prices_the_airline_from_the_rate_card(ctx, offline, failure):  # noqa: F811
    """A dropped connection is no RatesUnavailable, and the lane list failed with the rates in rounds 46 and 47:
    neither asked the card. A 429 past the deadline did not either (`tools.outage_drill` portal_429, NEXT_CASES E1)."""
    if failure == "lane list":
        offline.get("/v1/portal/ports").respond(503, json={"detail": "The rate API is unavailable."})
    elif failure == "rate limit":
        offline.get("/v1/portal/ports").respond(200, json=PORTS)
        offline.get("/v1/portal/rates").respond(429, headers={"Retry-After": "3600"}, json={"detail": "slow down"})
    else:
        offline.get("/v1/portal/ports").respond(200, json=PORTS)
        offline.get("/v1/portal/rates").mock(side_effect=httpx.ConnectError("connection reset"))
    db.put(ctx.db, "rate_card.breaks.A1", '["0", "1000"]')
    events.emit(ctx, rate_card.KIND, "pricing", payload={
        "origin": "AAA", "destination": "BBB", "code": "A1", "carrier": "Air One", "kg": "100", "total": "650.00",
        "currency": "EUR", "fx_eur": None, "transit_days": 2, "version": "v1"})  # fmt: skip
    found = await portal_rates.options(ctx, rfq(), deadline())
    assert [(o.carrier, o.total_eur, o.rate_id) for o in found] == [("Air One", D("650.00"), None)]


@pytest.mark.parametrize("kg", ["1000", "800"])
async def test_the_card_gives_no_price_for_an_airline_the_current_version_found_off_the_portal(ctx, offline, kg):  # noqa: F811
    """A v1 rate scaled by Air Two's factor priced Air One at 3900.00, although Air One's read at v2 answered 404; at
    800 kg the same rate came in as the cheaper higher break."""
    answer_rates(offline, "503")
    for code, name, version, read_kg, total in [
        ("A1", "Air One", "v1", "100", "650.00"), ("A1", "Air One", "v1", "1000", "3000.00"),
        ("A2", "Air Two", "v1", "100", "650.00"),
        ("A2", "Air Two", "v2", "100", "845.00"), ("A1", "Air One", "v2", "1000", None),
    ]:  # fmt: skip
        db.put(ctx.db, f"rate_card.breaks.{code}", '["0", "1000"]')
        events.emit(ctx, rate_card.KIND, "pricing", payload={
            "origin": "AAA", "destination": "BBB", "code": code, "carrier": name, "kg": read_kg, "total": total,
            "currency": "EUR", "fx_eur": None, "transit_days": 2, "version": version})  # fmt: skip
    found = await portal_rates.options(ctx, rfq().model_copy(update={"weight_kg": D(kg)}), deadline())
    assert "Air One" not in [o.carrier for o in found]


async def test_the_rate_card_reads_the_bracket_requests_need_once_the_request_is_answered(ctx, offline, monkeypatch):  # noqa: F811
    answer_rates(offline, "healthy")
    offline.get("/v1/portal/brackets").respond(200, json={"breaks_kg": [0, 1000]})
    monkeypatch.setattr(rate_card, "PACE_S", 0)
    received = timing.now() - timedelta(seconds=30)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=7, payload={"received_at": received.isoformat(),
                                                                    "weight_kg": "1200", "volume_cbm": "1"})  # fmt: skip

    async def fill() -> list[httpx.Request]:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(rate_card.run(ctx), 0.2)
        return [call.request for call in offline.calls if call.request.url.path == "/v1/portal/rates"]

    assert await fill() == []
    events.emit(ctx, "quote.sent", "outbox", session_id=7, payload={})
    reads = await fill()  # one a minute while no read has named the limit
    assert [(r.url.params["airline"], r.url.params["weight_kg"]) for r in reads] == [("A1", "1000")]
