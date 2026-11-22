"""Pricing regressions from the end-to-end review. All sources are local or simulated."""

import asyncio
import io
import json
from datetime import date, timedelta
from decimal import Decimal as D

import httpx
import pytest
from PIL import Image

from pixel.agents.rfq import compose
from pixel.agents.tariffs import agent as tariffs
from pixel.api.client import ApiError
from pixel.api.portal import Lane, _option
from pixel.contracts import LaneTariff, LlmResult, Preference, Source
from pixel.core import events
from pixel.pricing import bids, calc, engine, portal_rates, selection, session, store
from tests.test_bids import FakePortal, deadline, kinds, mail, rfq
from tests.test_outbox import make_ctx
from tests.test_portal import RATE
from tests.test_tariffs import FIXTURES


@pytest.fixture
def ctx(conn):
    context = make_ctx(conn)
    context.portal = FakePortal()
    return context


@pytest.mark.parametrize(
    "offer, amount, eur, days",
    [
        ("Air freight, all-in EUR 2.80/kg. Transit 48 hours.", "280.00", "280.00", 2),
        ("Freight per kg: EUR 2.80. Transit 48 hours.", "280.00", "280.00", 2),
        ("All-in total EUR 1,000.00 including fuel EUR 100.00. Transit 3 days.", "1000.00", "1000.00", 3),
        ("Total USD 100.00; 1 USD = 0.9150 EUR. Transit time 49 hours.", "100.00", "91.50", 3),
        ("Our all-in rate is 2,835 EUR per kg. Transit time 2 days.", "283.50", "283.50", 2),
    ],
)
def test_carrier_amount_and_unit_reach_the_selected_customer_quote(offer, amount, eur, days):
    request = rfq(preference=Preference.CHEAPEST)
    bid = bids.parse(mail("air@x", offer), request)
    assert (bid.total, bid.total_eur, bid.transit_days) == (D(amount), D(eur), days)
    option = selection.select(request, [bid])
    _, body = compose.render(request, option, None)
    assert f"{amount} {bid.currency}" in body


def test_unrecognized_rate_basis_is_not_a_flat_bid():
    assert bids.parse(mail("air@x", "EUR 12.20 per pallet. Transit 3 days."), rfq()) is None


def test_unit_rate_uses_chargeable_weight():
    request = rfq(volume_cbm=D("2.0"))
    bid = bids.parse(mail("air@x", "All-in rate EUR 2.80/kg. Transit 3 days."), request)
    assert (bid.chargeable_kg, bid.total) == (D(334), D("935.20"))


async def test_model_transcribes_rates_and_duration_for_code_to_calculate(ctx, monkeypatch):
    async def transcribe(*args, **kwargs):
        return LlmResult(
            data={
                "offer": True,
                "total": None,
                "components": ["2.835 USD/kg", "USD 16.50"],
                "currency": "USD",
                "eur_per_unit": "0.9150",
                "transit_days": "48 hours",
            },
            model="test",
            tokens_in=1,
            tokens_out=1,
            cost_usd=D(0),
        )

    monkeypatch.setattr(bids.llm, "ask_json", transcribe)
    option = await bids._read_with_model(ctx, mail("air@x", "offer"), rfq(), True)
    assert (option.total, option.total_eur, option.transit_days) == (D("300.00"), D("274.50"), 2)


@pytest.mark.parametrize("failure", ["503", "slow"])
async def test_carriers_can_still_price_an_unavailable_portal(ctx, monkeypatch, failure):
    asked = asyncio.Event()
    store.save_carriers(
        ctx, [{"name": n, "kind": "airline", "email": f"{n}@test", "serves": rfq().destination} for n in ("one", "two")]
    )

    async def lanes():
        if failure == "503":
            raise ApiError(503, "unavailable")
        await asyncio.Event().wait()

    async def send_requests(context, request, candidates):
        assert len(candidates) == 2
        asked.set()
        return [11, 12]

    async def collect(*args):
        return [bids.parse(mail("air@x", "All-in EUR 500. Transit 3 days."), rfq())]

    monkeypatch.setattr(ctx.portal, "lanes", lanes)
    monkeypatch.setattr(bids, "request", send_requests)
    monkeypatch.setattr(bids, "collect", collect)
    monkeypatch.setattr(engine, "PORTAL_HEAD_START_S", 0.01)
    found = await asyncio.wait_for(engine.options(ctx, rfq(), deadline(0.12)), timeout=1)
    assert asked.is_set() and [o.total for o in found] == [D("500.00")]


class ChangingPortal:
    def __init__(self):
        self.version = "one"
        self.rate_remaining = 0
        self.reads = []
        self.bracket_reads = []

    async def lanes(self):
        return [Lane("AAA", "BBB", (("A", "Air A"), ("B", "Air B")))]

    async def brackets(self, airline):
        self.bracket_reads.append((airline, self.version))
        return (0, 200 if self.version == "one" else 150)

    async def rate(self, origin, destination, airline, weight_kg, volume_cbm, **kw):
        self.reads.append((airline, weight_kg))
        self.rate_remaining = 15
        prices = {100: "300", 150: "200", 200: "250"}
        return _option(
            {
                **RATE,
                "currency": "EUR",
                "airline": airline,
                "airline_code": airline,
                "chargeable_weight_kg": str(weight_kg),
                "dataset_version": self.version,
                "total": prices[weight_kg],
            }
        )


async def test_portal_rechecks_quota_and_uses_the_current_dataset_break(ctx):
    ctx.portal = portal = ChangingPortal()
    first = await portal_rates.options(ctx, rfq(), deadline())
    assert {o.carrier for o in first} == {"A", "B"}
    assert all(o.billed_kg == 200 and o.total == 250 for o in first)
    assert (await portal_rates.refresh(ctx, first[0])).billed_kg == 200
    portal.version = "two"
    second = await portal_rates.options(ctx, rfq(), deadline())
    assert all(o.billed_kg == 150 and o.total == 200 for o in second)
    assert set(portal.bracket_reads) == {("A", "one"), ("A", "two"), ("B", "one"), ("B", "two")}


async def test_one_portal_carrier_timeout_keeps_the_other_price(ctx, monkeypatch):
    ctx.portal = portal = ChangingPortal()
    rate = portal.rate

    async def partial_outage(origin, destination, airline, *args, **kw):
        if airline == "A":
            raise httpx.ReadTimeout("offline test")
        return await rate(origin, destination, airline, *args, **kw)

    monkeypatch.setattr(portal, "rate", partial_outage)
    assert [o.carrier for o in await portal_rates.options(ctx, rfq(), deadline())] == ["B"]
    assert kinds(ctx, "option.failed")[0]["error"] == "ReadTimeout"


async def test_scanned_pdf_renders_each_page_and_validates_before_activation(ctx, monkeypatch):
    model_pages = json.loads((FIXTURES / "danube-model-pages.json").read_text())
    calls = []

    async def transcribe(*args, **kwargs):
        (image,) = kwargs["images"]
        assert Image.open(io.BytesIO(image)).format == "JPEG"
        page = model_pages[len(calls)]
        calls.append(image)
        return LlmResult(data=page, model="test", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(tariffs.llm, "ask_json", transcribe)
    pdf = io.BytesIO()
    page = Image.new("RGB", (90, 80), "white")
    page.save(pdf, format="PDF", save_all=True, append_images=[page])
    files = [("scan.pdf", pdf.getvalue())]
    await tariffs._ingest(ctx, "scan.pdf", files)
    assert len(calls) == 2 and len(kinds(ctx, "tariff.ingested")) == 1
    lane = model_pages[0]["lanes"][0]
    on = date.fromisoformat(model_pages[0]["valid_from"])
    assert store.lookup(ctx, lane["origin"], lane["destination"], on)
    await tariffs._ingest(ctx, "scan.pdf", files)
    assert len(calls) == 2

    calls.clear()
    model_pages[0]["lanes"][0]["minimum"] = "-1"
    await tariffs._ingest(ctx, "bad-scan.pdf", files)
    assert len(calls) == 2 and len(kinds(ctx, "tariff.failed")) == 1
    assert not store.has_source(ctx, "bad-scan.pdf")


@pytest.mark.parametrize(
    "mail_says, portal_fx, fx",
    [
        ("All rates in USD. Exchange rate: 1 USD = 0.95 EUR.", "0.92", "0.95"),  # the tariff mail's own rate
        ("", "0.92", "0.92"),  # the newest rate the portal quoted for USD
        ("", None, None),  # no rate anywhere: not priced, never a constant
    ],
)
async def test_a_usd_sheet_is_priced_with_its_stated_or_the_newest_portal_rate(ctx, mail_says, portal_fx, fx):
    sheet = (FIXTURES / "variants" / "testline-cargo-usd.txt").read_bytes()
    await tariffs._ingest(ctx, "usd.txt", [("usd.txt", sheet)], mail=mail_says)
    for rate in ("0.80", portal_fx) if portal_fx else ():
        option = {"source": "portal", "currency": "USD", "fx_eur": rate}
        events.emit(ctx, "option.found", "pricing", session_id=9, payload={"option": option})
    request = rfq().model_copy(update={"origin": "CGN", "destination": "SYD"})
    [option] = engine.sheet_options(ctx, request) or [None]
    if fx is None:
        assert option is None
    else:
        assert (option.currency, option.fx_eur, option.total_eur) == ("USD", D(fx), calc.money(option.total * D(fx)))


def test_future_shipment_uses_the_tariff_valid_on_its_ready_day(ctx):
    request = rfq()
    today = request.received_at.date()
    future = request.model_copy(update={"ready_on": today + timedelta(days=7)})
    old = LaneTariff(
        carrier="Example Air", origin=future.origin, destination=future.destination, currency="EUR",
        valid_from=today, valid_to=today + timedelta(days=6), version="old", transit_days=3,
        minimum=D(100), breaks=((D(0), D(1)),), fuel_per_kg=D(0), security_per_kg=D(0), source="old.txt",
    )  # fmt: skip
    store.upsert(ctx, [old])
    [old_option] = engine.sheet_options(ctx, request)
    assert session.for_request(ctx, future) == []
    assert engine.sheet_options(ctx, future) == []

    portal = old_option.model_copy(update={"source": Source.PORTAL})
    events.emit(ctx, "option.found", "pricing", session_id=future.session_id,
                payload={"option": portal.model_dump(mode="json")})  # fmt: skip
    revised = old.model_copy(update={"valid_from": future.ready_on, "valid_to": future.ready_on,
                                     "version": "revised", "minimum": D(200)})  # fmt: skip
    store.upsert(ctx, [revised])
    [sheet] = engine.sheet_options(ctx, future)
    assert (sheet.dataset_version, sheet.total) == ("revised", D(200))
    assert session.for_request(ctx, future) == [portal, sheet]


def test_an_expired_sheet_is_the_last_resort_for_a_cargo_ready_after_it(ctx):
    """Next case 5 without a portal rate or a bid on the lane: the expired sheet still answers (R1), but only then,
    and it is never logged before, so its airline is still asked for a bid."""
    request = rfq()
    today = request.received_at.date()
    future = request.model_copy(update={"ready_on": today + timedelta(days=7)})
    store.upsert(ctx, [LaneTariff(
        carrier="Example Air", origin=future.origin, destination=future.destination, currency="EUR",
        valid_from=today, valid_to=today + timedelta(days=6), version="old", transit_days=3,
        minimum=D(100), breaks=((D(0), D(1)),), fuel_per_kg=D(0), security_per_kg=D(0), source="old.txt",
    )])  # fmt: skip
    assert engine.expired_sheet_options(ctx, request) == []  # ready today: the sheet is valid
    [expired] = engine.expired_sheet_options(ctx, future)
    assert (expired.carrier, expired.dataset_version) == ("Example Air", "old")
    assert kinds(ctx, "option.found") == []
