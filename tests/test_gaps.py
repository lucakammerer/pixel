"""Known gaps (`agents/rfq/gaps.py`): what the session knows it has not read, closed while it can change the quote."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_check import airline, rfq
from test_outbox import ingest_rfq, make_ctx

from pixel.agents.rfq import agent, gaps, novelty
from pixel.contracts import Preference
from pixel.core import events, timing
from pixel.core.orchestrator import Deadline
from pixel.pricing import portal_rates

LANE = SimpleNamespace(origin="FRA", destination="ORD", airlines=[("XC", "Xenon Cargo Airlines"), ("AU", "Aurelia")])


def _setup(conn, **kw):
    ctx = make_ctx(conn, **kw)
    mail = ingest_rfq(ctx)
    request = rfq(session_id=mail.session_id, message_id=mail.id, customer=mail.sender, preference=Preference.FASTEST)
    events.emit(ctx, "rfq.extracted", "rfq", session_id=request.session_id, payload=request.model_dump(mode="json"))
    return ctx, request


def _found(ctx, request, option):
    events.emit(ctx, "option.found", "pricing", session_id=request.session_id,
                payload={"option": option.model_dump(mode="json")})  # fmt: skip


def _failed(ctx, request, carrier, error="RatesUnavailable"):
    events.emit(ctx, "option.failed", "pricing", session_id=request.session_id,
                payload={"source": "portal", "carrier": carrier, "error": error})  # fmt: skip


def _asked(ctx, request, carrier, message_id, pickup=False):
    events.emit(ctx, "bid.requested", "pricing", session_id=request.session_id, message_id=message_id,
                payload={"carrier": carrier, "email": f"{message_id}@x", "pickup": pickup, "origin": "FRA",
                         "destination": "ORD"})  # fmt: skip


def _deadline():
    now = timing.now()
    return Deadline(now, now, now + timedelta(seconds=30), now + timedelta(seconds=60))


def _gaps_event(conn):
    rows = conn.execute("SELECT payload FROM events WHERE kind = 'quote.gaps'").fetchall()
    return [json.loads(r[0]) for r in rows]


@pytest.fixture
def portal(monkeypatch):
    """A portal whose rate API is down for the first `down` reads of each airline."""
    state = {"down": 1, "reads": {}}

    async def lanes(ctx, **kw):
        return [LANE]

    async def read(ctx, rfq_, deadline, code, name):
        state["reads"][name] = state["reads"].get(name, 0) + 1
        if state["reads"][name] <= state["down"]:
            _failed(ctx, rfq_, name)
            return None
        option = airline("1550.00", name, days=1)
        _found(ctx, rfq_, option)
        return option

    monkeypatch.setattr(portal_rates, "lanes", lanes)
    monkeypatch.setattr(portal_rates, "_read", read)
    monkeypatch.setattr(gaps, "REREAD_GAP_S", 0.05)
    return state


def test_a_failed_portal_read_is_a_gap_and_not_on_the_portal_is_none(conn):
    ctx, request = _setup(conn)
    _found(ctx, request, airline("1483.43", "Sheet Air", days=4))
    _failed(ctx, request, "Aurelia")
    _failed(ctx, request, "Xenon Cargo Airlines", error="NotOnPortal")
    assert [(g.kind, g.name, g.can_change) for g in gaps.find(ctx, request)] == [("source_down", "Aurelia", True)]


def test_a_failed_read_of_an_airline_the_customer_ruled_out_cannot_change_the_quote(conn):
    ctx, request = _setup(conn)
    request = request.model_copy(update={"excluded_carriers": ("Aurelia",)})
    _failed(ctx, request, "Aurelia")
    assert [g.can_change for g in gaps.find(ctx, request)] == [False]


async def test_an_outage_that_ends_mid_round_is_read_again_and_priced(conn, portal):
    """Rounds 120 and 121: the rate API answered 503, the sheet airline was quoted, the faster one was lost."""
    ctx, request = _setup(conn)
    ctx.portal = SimpleNamespace(rate_remaining=None)
    sheet = airline("1483.43", "Sheet Air", days=4)
    _found(ctx, request, sheet)
    _failed(ctx, request, "Aurelia")
    _, options = await agent._close_gaps(ctx, request, [sheet], _deadline(), "")
    assert gaps.find(ctx, request) == []
    [event] = _gaps_event(conn)
    assert event["outage"] and event["changed"] and event["open"] == [] and portal["reads"] == {"Aurelia": 2}
    assert {o.carrier for o in options} == {"Aurelia", "Sheet Air"}
    assert agent.selection.select(request, options).carrier == "Aurelia"  # fastest: one day against four


async def test_an_outage_that_lasts_is_read_again_at_most_rereads_times_and_the_quote_goes(conn, portal):
    ctx, request = _setup(conn)
    ctx.portal = SimpleNamespace(rate_remaining=None)
    portal["down"] = 99
    _found(ctx, request, airline("1483.43", "Sheet Air", days=4))
    _failed(ctx, request, "Aurelia")
    now = timing.now()
    started = timing.now()
    _, _changed = await gaps.close(ctx, request, _deadline(), by=now, outage_by=now + timedelta(seconds=1))
    assert portal["reads"] == {"Aurelia": gaps.REREADS}
    assert (timing.now() - started).total_seconds() < 1.5
    [event] = _gaps_event(conn)
    assert [g["kind"] for g in event["open"]] == ["source_down"]


async def test_a_pending_answer_is_waited_for_on_a_thin_price_basis_and_stops_when_it_comes(conn):
    ctx, request = _setup(conn)
    _found(ctx, request, airline("1483.43", "Sheet Air"))
    _asked(ctx, request, "Mail Air", 777)

    async def answer():
        await asyncio.sleep(0.2)
        events.emit(ctx, "bid.received", "pricing", session_id=request.session_id,
                    payload={"carrier": "Mail Air", "request_id": 777, "origin": "FRA", "destination": "ORD"})  # fmt: skip

    asyncio.get_running_loop().create_task(answer())
    started = timing.now()
    now = timing.now()
    await gaps.close(ctx, request, _deadline(), by=now + timedelta(seconds=3), outage_by=now)
    waited = (timing.now() - started).total_seconds()
    assert 0.15 < waited < 1.5 and _gaps_event(conn)[0]["open"] == []


async def test_a_pending_answer_is_not_waited_for_when_enough_airlines_are_priced(conn):
    ctx, request = _setup(conn)
    _found(ctx, request, airline("1483.43", "Sheet Air"))
    _found(ctx, request, airline("1500.00", "Other Air"))
    _asked(ctx, request, "Mail Air", 777)
    started, now = timing.now(), timing.now()
    await gaps.close(ctx, request, _deadline(), by=now + timedelta(seconds=3), outage_by=now)
    assert (timing.now() - started).total_seconds() < 0.3
    assert [g["kind"] for g in _gaps_event(conn)[0]["open"]] == ["answer_pending"]


async def test_sentences_whose_mapping_failed_are_mapped_once_more(conn, monkeypatch):
    ctx, request = _setup(conn)
    events.emit(ctx, "rfq.novelty", "rfq", session_id=request.session_id,
                payload={"leftover": ["Please avoid Aurelia."], "mapped": {"error": "TimeoutError"}})  # fmt: skip
    calls = []

    async def check(ctx_, rfq_, text, subject=""):
        calls.append(text)
        return rfq_.model_copy(update={"excluded_carriers": ("Aurelia",)})

    monkeypatch.setattr(novelty, "check", check)
    monkeypatch.setattr(gaps, "NOVELTY_MIN_S", 0.0)
    now = timing.now()
    got, _ = await gaps.close(ctx, request, _deadline(), by=now + timedelta(seconds=1), outage_by=now,
                              text="Please avoid Aurelia.")  # fmt: skip
    assert calls == ["Please avoid Aurelia."] and got.excluded_carriers == ("Aurelia",)


async def test_log_mode_records_the_gaps_and_waits_for_nothing(conn, portal):
    ctx, request = _setup(conn, gaps="log")
    ctx.portal = SimpleNamespace(rate_remaining=None)
    sheet = airline("1483.43", "Sheet Air", days=4)
    _found(ctx, request, sheet)
    _failed(ctx, request, "Aurelia")
    _got, options = await agent._close_gaps(ctx, request, [sheet], _deadline(), "")
    assert options == [sheet] and portal["reads"] == {}
    assert _gaps_event(conn)[0]["logged_only"] is True


async def test_no_gap_no_event_and_no_wait(conn):
    ctx, request = _setup(conn)
    _found(ctx, request, airline("1483.43"))
    got, changed = await gaps.close(ctx, request, _deadline(), by=timing.now(), outage_by=timing.now())
    assert got == request and not changed and _gaps_event(conn) == []


async def test_a_broken_gap_lookup_never_costs_the_quote(conn, monkeypatch):
    ctx, request = _setup(conn)

    def broken(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(gaps, "_find", broken)
    got, changed = await gaps.close(ctx, request, _deadline(), by=timing.now(), outage_by=timing.now())
    assert got == request and not changed
    assert conn.execute("SELECT 1 FROM events WHERE kind = 'error'").fetchone()


def test_the_wait_without_a_failed_source_ends_with_the_full_time_points():
    now = timing.now()
    deadline = Deadline(now, now, now + timedelta(seconds=315), now + timedelta(seconds=360))
    assert gaps.by_for(deadline) == now + timedelta(seconds=gaps.FULL_TIME_S)
    assert Decimal(gaps.FULL_TIME_S) < Decimal(120)


async def test_the_handler_keeps_its_repair_time_after_a_long_gap_wait(conn, monkeypatch):
    """An outage wait ends at 15 s before the orchestrator takes over; what follows must not take that moment for a
    request first seen after the takeover and plan until `hard_at`."""
    from test_bids import IDS, D, LaneTariff, date, mail

    from pixel.agents.rfq import agent as rfq_agent
    from pixel.core import outbox
    from pixel.crm import quotes
    from pixel.pricing import store

    ctx = make_ctx(conn, model_check="")
    ctx.db.execute("DELETE FROM carriers")
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    start = timing.now()
    window = Deadline(start, start, start + timedelta(seconds=315), start + timedelta(seconds=360))  # takeover at 150 s
    late = start + timedelta(seconds=200)
    seen = {}

    async def close(ctx_, rfq_, deadline_, **kw):
        timing.clock.set(lambda: late)  # the wait took us past the takeover
        seen["outage_by"] = kw["outage_by"]
        return rfq_, False

    async def settled(ctx_, rfq_, options, prose, **kw):
        seen["repair_by"] = kw["repair_by"]
        raise RuntimeError("stop here")

    async def send_quote(ctx_, quote):
        return quote

    async def options(ctx_, rfq_, deadline_):
        return [airline("260.00", "Air One").model_copy(update={"origin": "AAA", "destination": "BBB"})]

    from pixel.pricing import engine

    monkeypatch.setattr(engine, "options", options)
    monkeypatch.setattr(gaps, "close", close)
    monkeypatch.setattr(rfq_agent, "_settled", settled)
    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    events.emit(ctx, "bid.requested", "pricing", session_id=1, message_id=5,
                payload={"carrier": "Mail Air", "email": "m@x", "pickup": False, "origin": "AAA",
                         "destination": "BBB"})  # fmt: skip
    with pytest.raises(RuntimeError, match="stop here"):
        await rfq_agent.handle(ctx, mail("c@x", "Please quote AAA-BBB, 100 kg, 0.3 cbm.", session_id=1), window)
    assert seen["repair_by"] == seen["outage_by"] <= window.recovery_at - timedelta(seconds=rfq_agent.REPAIR_MARGIN_S)


async def test_a_read_error_that_is_no_outage_is_read_again_only_inside_the_full_time(conn, portal):
    ctx, request = _setup(conn)
    ctx.portal = SimpleNamespace(rate_remaining=None)
    portal["down"] = 99
    _found(ctx, request, airline("1483.43", "Sheet Air", days=4))
    _failed(ctx, request, "Aurelia", error="ApiError")
    [gap] = gaps.find(ctx, request)
    assert gap.kind == "source_down" and gap.can_change and not gap.outage
    now = timing.now()
    started = timing.now()
    await gaps.close(ctx, request, _deadline(), by=now + timedelta(seconds=0.1), outage_by=now + timedelta(seconds=3))
    assert (timing.now() - started).total_seconds() < 1.0 and not _gaps_event(conn)[0]["outage"]
