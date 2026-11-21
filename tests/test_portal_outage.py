"""Rounds 27 to 29: the portal's rate API answered 503 "Rates are still published on the portal page". Offline: the
portal is mocked at HTTP level, carriers and the rate desk answer through `bids.record`."""

import asyncio
from datetime import date
from decimal import Decimal as D

import httpx
import pytest
import respx

from pixel import llm
from pixel.api.mailbox import RATEDESK
from pixel.api.portal import Portal
from pixel.contracts import LaneTariff, Preference, Source
from pixel.core import events
from pixel.pricing import bids, engine, selection, session, store
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
DECLINE = "We do not serve this lane/zone: AAA-BBB. We fly AAA-CCC, DDD-EEE."


@pytest.fixture(autouse=True)
def offline(ctx, monkeypatch):  # noqa: F811
    async def no_model(*args, **kw):
        raise AssertionError("no model call on this path")

    monkeypatch.setattr(llm, "ask_json", no_model)  # bids.record catches it as an `error` event
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
    elif case == "503":
        route.respond(503, json={"detail": "Rates are still published on the portal page"})


def desk_asked(ctx) -> list[dict]:  # noqa: F811
    return [r for r in kinds(ctx, "bid.requested") if r["email"] == RATEDESK]


async def wait_for(ctx, n: int) -> None:  # noqa: F811
    for _ in range(50):
        if len(kinds(ctx, "bid.requested")) >= n:
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "case, airlines_mailed, desk",
    [("healthy", 0, 0), ("no lane", 2, 0), ("404", 2, 0), ("429", 2, 0), ("503", 2, 1)],
)
async def test_only_a_rate_api_outage_asks_the_rate_desk(ctx, offline, case, airlines_mailed, desk):  # noqa: F811
    answer_rates(offline, case)
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    found = await engine.options(ctx, request, deadline(0.3))
    asked = kinds(ctx, "bid.requested")
    assert (len(asked) - len(desk_asked(ctx)), len(desk_asked(ctx))) == (airlines_mailed, desk)
    assert {o.source for o in found} == ({Source.PORTAL} if case == "healthy" else set())


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


def at_weight(kg: str, **kw):
    return rfq(**kw).model_copy(update={"weight_kg": D(kg)})


def reply(ctx, email: str, body: str):  # noqa: F811
    """An answer to our request at a weight break, known by the mail it replies to."""
    (enquiry,) = ctx.db.execute(
        "SELECT message_id FROM events WHERE kind='bid.requested' AND json_extract(payload,'$.email')=? "
        "AND json_extract(payload,'$.requested_kg') IS NOT NULL",
        (email,),
    ).fetchone()
    return mail(email, body).model_copy(update={"in_reply_to": enquiry})


def at_break(ctx) -> dict[str, str]:  # noqa: F811
    return {r["carrier"]: r["requested_kg"] for r in kinds(ctx, "bid.requested") if "requested_kg" in r}


@pytest.mark.parametrize(
    ("case", "weight", "breaks", "asked"),
    [
        ("503", "859", [0, 100, 300, 500, 1000], {"Air One": "1000", "Air Two": "1000", "Rate desk": "1000"}),
        ("503", "600", [0, 100, 300, 500, 1000], {}),  # 600 of 1000 kg: below the share worth asking
        ("503", "859", [0, 100, 300, 500], {}),  # no break above the weight
        ("404", "859", [0, 100, 300, 500, 1000], {}),  # the rate API is up: no outage
        ("no lane", "859", [0, 100, 300, 500, 1000], {}),  # mail-only airlines have no portal breaks
    ],
)
async def test_a_rate_outage_also_asks_the_next_weight_break_once(ctx, offline, case, weight, breaks, asked):  # noqa: F811
    answer_rates(offline, case)
    offline.get("/v1/portal/brackets").respond(200, json={"breaks_kg": breaks})
    request = at_weight(weight)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    for _ in range(2):  # a second pass (a restart, a new extraction) asks no break again
        await engine.options(ctx, request, deadline(0.2))
    assert at_break(ctx) == asked
    assert len([r for r in kinds(ctx, "bid.requested") if "requested_kg" in r]) == len(asked)


async def test_a_bid_at_the_next_break_is_billed_at_it_and_wins_when_cheaper(ctx, offline):  # noqa: F811
    answer_rates(offline, "503")
    offline.get("/v1/portal/brackets").respond(200, json={"breaks_kg": [0, 300, 1000]})
    request = at_weight("859", preference=Preference.CHEAPEST)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))

    async def answer():
        await wait_for(ctx, 6)
        await bids.record(ctx, mail("one@air", "All-in EUR 2,400.00, transit 3 days"))  # known by its sender only
        await bids.record(ctx, reply(ctx, "one@air", "All-in EUR 2,300.00, transit 3 days\n"
                                                     "Please quote reference R-one1000 when booking."))  # fmt: skip
        await bids.record(ctx, reply(ctx, "two@air", "EUR 2.50 per kg all-in, transit 2 days"))
        await bids.record(ctx, reply(ctx, RATEDESK, "- Air Two (A2): 2,600.00 EUR, 2 days, rate_id R-desk2, "
                                                    "valid until 21 Nov 2026 18:38 UTC"))  # fmt: skip

    found, _ = await asyncio.gather(engine.options(ctx, request, deadline(1)), answer())
    by = {(o.carrier, o.source, o.billed_kg): o for o in found}
    assert by[("Air One", Source.BID, D(859))].total == D("2400.00")
    assert by[("Air Two", Source.BID, D(1000))].total == D("2500.00")  # the rate per kg times the break
    assert by[("Air Two", Source.RATEDESK, D(1000))].rate_id == "R-desk2"
    best = selection.select(request, found)
    assert (best.carrier, best.total, best.billed_kg, best.chargeable_kg, best.rate_id) == (
        "Air One", D("2300.00"), D(1000), D(859), "R-one1000",
    )  # fmt: skip
    assert [r.get("requested_kg") for r in kinds(ctx, "bid.received")] == [None, "1000", "1000", "1000"]


def portal_option(ctx, session_id: int, version: str = "v1", **kw) -> None:  # noqa: F811
    events.emit(ctx, "option.found", "pricing", session_id=session_id, payload={"option": {
        "source": "portal", "carrier": "Air One", "origin": "AAA", "destination": "BBB", "chargeable_kg": "100",
        "billed_kg": "100", "freight": "650", "total": "650", "currency": "EUR", "total_eur": "650",
        "transit_days": 3, "dataset_version": version, **kw}})  # fmt: skip


def tariff(carrier: str, origin: str, destination: str) -> LaneTariff:
    return LaneTariff(carrier=carrier, origin=origin, destination=destination, currency="EUR",
                      valid_from=date(2000, 1, 1), valid_to=date(2100, 1, 1), version="t", transit_days=3,
                      minimum=D(1), breaks=((D(0), D(9)),), fuel_per_kg=D(0), security_per_kg=D(0), source="test")  # fmt: skip


async def answer_in(ctx, session_id: int, sender: str, body: str) -> None:  # noqa: F811
    request = rfq().model_copy(update={"session_id": session_id})
    events.emit(ctx, "rfq.extracted", "rfq", session_id=session_id, payload=request.model_dump(mode="json"))
    await bids.record(ctx, mail(sender, body, session_id=session_id))


def mailed(ctx) -> set[str]:  # noqa: F811
    return {r["carrier"] for r in kinds(ctx, "bid.requested")}


@pytest.mark.parametrize(
    ("where", "version", "skipped"),
    [
        ("same session", "v1", True),
        ("other session", "v1", True),  # the same portal dataset and tariffs
        ("other session", "v2", False),  # the portal's dataset changed since
        ("other session, tariffs changed", "v1", False),
        ("other session, priced since", "v1", False),
        (None, "v1", False),
    ],
)
async def test_an_airline_that_declined_with_its_lanes_is_not_asked_again(ctx, offline, where, version, skipped):  # noqa: F811
    ports = {"lanes": [{"origin": "AAA", "destination": "BBB", "airlines": [{"code": "A1", "name": "Air One"}]}]}
    offline.get("/v1/portal/ports").respond(200, json=ports)
    offline.get("/v1/portal/rates").respond(200, json={**RATE, "airline": "Air One", "airline_code": "A1",
                                                       "dataset_version": version})  # fmt: skip
    if where:
        session_id = 1 if where == "same session" else 2
        portal_option(ctx, session_id)  # the dataset the decline was read on
        await answer_in(ctx, session_id, "two@air", DECLINE)
        assert kinds(ctx, "bid.received")[-1]["served_lanes"] == ["AAA-CCC", "DDD-EEE"]
    if where == "other session, tariffs changed":
        store.upsert(ctx, [tariff("Air Three", "CCC", "DDD")])
    if where == "other session, priced since":
        await answer_in(ctx, 3, "two@air", "All-in EUR 640.00, transit 2 days")
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    await engine.options(ctx, request, deadline(0.2))
    assert ("Air Two" not in mailed(ctx)) == skipped
    if skipped:
        assert kinds(ctx, "option.failed")[-1] == {"source": "mail", "carrier": "Air Two",
                                                   "error": "not mailed: does not fly the lane"}  # fmt: skip


@pytest.mark.parametrize(
    ("airlines", "rates", "skipped"),
    [
        # Air Two is on the portal, for another lane only, while Air One prices this one.
        ([("AAA", "BBB", "A1", "Air One"), ("AAA", "CCC", "A2", "Air Two")], "healthy", True),
        # Nothing priced yet: everyone who may fly the lane is asked.
        ([("AAA", "BBB", "A1", "Air One"), ("AAA", "CCC", "A2", "Air Two")], "404", False),
        # On the portal for this lane: always asked.
        ([("AAA", "BBB", "A1", "Air One"), ("AAA", "BBB", "A2", "Air Two")], "404", False),
        ([("AAA", "BBB", "A1", "Air One")], "healthy", False),  # not on the portal at all: a mail-only airline
        ([], "ports down", False),
    ],
)
async def test_a_portal_airline_listed_only_on_other_lanes_is_not_mailed(ctx, offline, airlines, rates, skipped):  # noqa: F811
    lanes: dict = {}
    for origin, destination, code, name in airlines:
        lanes.setdefault((origin, destination), []).append({"code": code, "name": name})
    if rates == "ports down":
        store.upsert(ctx, [tariff("Air One", "AAA", "BBB")])  # an option, so only the missing ports keep Air Two
        offline.get("/v1/portal/ports").respond(503, json={"detail": "down"})
    else:
        ports = {"lanes": [{"origin": o, "destination": d, "airlines": a} for (o, d), a in lanes.items()]}
        offline.get("/v1/portal/ports").respond(200, json=ports)
    route = offline.get("/v1/portal/rates")
    if rates == "healthy":
        route.respond(200, json={**RATE, "airline": "Air One", "airline_code": "A1"})
    else:
        route.respond(404, json={"detail": "Email the airline."})
    request = rfq()
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, payload=request.model_dump(mode="json"))
    await engine.options(ctx, request, deadline(0.2))
    assert ("Air Two" not in mailed(ctx)) == skipped


async def test_a_decline_never_hides_an_airline_whose_portal_read_answered_503(ctx, offline):  # noqa: F811
    answer_rates(offline, "503")
    store.upsert(ctx, [tariff("Air Three", "AAA", "BBB")])  # an option, so a skip would be allowed
    await answer_in(ctx, 1, "two@air", DECLINE)
    await engine.options(ctx, rfq(), deadline(0.2))
    assert {"Air One", "Air Two", "Rate desk"} <= mailed(ctx)


async def test_a_bid_in_another_currency_takes_the_exchange_rate_of_its_own_request(ctx):  # noqa: F811
    for session_id in (1, 2):
        request = rfq().model_copy(update={"session_id": session_id})
        events.emit(ctx, "rfq.extracted", "rfq", session_id=session_id, payload=request.model_dump(mode="json"))
    portal_option(ctx, 1, currency="USD", total="700", total_eur="640.50", fx_eur="0.9150")
    await bids.record(ctx, mail("one@air", "All-in USD 1000.00, transit 3 days", session_id=1))
    portal_option(ctx, 2, currency="GBP", total="700", total_eur="805.00", fx_eur="1.15")  # another session's rate
    await bids.record(ctx, mail("two@air", "All-in GBP 800.00, transit 3 days", session_id=1))
    await bids.record(ctx, mail("two@air", "All-in GBP 800.00, transit 3 days", session_id=2))
    own = [o for o in session.options(ctx, 1) if o.source == Source.BID]
    assert [(o.carrier, o.total_eur, o.fx_eur) for o in own] == [("Air One", D("915.00"), D("0.9150"))]
    assert [o.total_eur for o in session.options(ctx, 2) if o.source == Source.BID] == [D("920.00")]
