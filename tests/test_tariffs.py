import io
import json
import re
import zipfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.tariffs import agent
from pixel.config import settings
from pixel.contracts import LlmResult
from pixel.core import events
from pixel.core.context import Context
from pixel.pricing import store

FIXTURES = Path(__file__).parent / "fixtures" / "tariffs"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())


@pytest.fixture
def ctx(conn, monkeypatch):
    emitted = []
    monkeypatch.setattr(events, "emit", lambda ctx, kind, actor, **kw: emitted.append((kind, kw.get("payload"))))
    context = Context(settings=settings(), db=conn, mailbox=None, portal=None, gateway=None, board=None)
    context.emitted = emitted
    return context


@pytest.mark.parametrize("filename", sorted(EXPECTED["files"]))
def test_every_code_parsed_sheet(start_kit, filename):
    tariffs = agent.parse(filename, (start_kit / filename).read_bytes())
    want = EXPECTED["files"][filename]
    assert (tariffs[0].carrier, len(tariffs)) == (want["carrier"], want["lanes"])
    assert {(t.currency, t.valid_from, t.valid_to) for t in tariffs} == {("EUR", date(2026, 11, 1), date(2026, 11, 30))}
    assert agent.check(tariffs) == []


@pytest.mark.parametrize("want", EXPECTED["lanes"], ids=lambda w: f"{w['file']}:{w['lane']}")
def test_hand_checked_lanes(start_kit, want):
    tariffs = agent.parse(want["file"], (start_kit / want["file"]).read_bytes())
    t = next(t for t in tariffs if f"{t.origin}-{t.destination}" == want["lane"])
    assert (t.transit_days, t.minimum, t.fuel_per_kg, t.security_per_kg) == (
        want["transit"], D(want["minimum"]), D(want["fuel"]), D(want["security"]),
    )  # fmt: skip
    assert t.breaks == tuple((D(k), D(r)) for k, r in want["breaks"])


def test_check_flags_rates_rising_with_weight(start_kit):
    t = agent.parse("x.txt", (start_kit / "yellowtail-air-freight-tariff-2026-11.txt").read_bytes())[0]
    broken = t.model_copy(update={"breaks": ((D(0), D("2.00")), (D(45), D("3.00")))})
    assert agent.check([broken])


def test_check_flags_a_minimum_that_looks_like_transit_days(start_kit):
    t = agent.parse("x.txt", (start_kit / "yellowtail-air-freight-tariff-2026-11.txt").read_bytes())[0]
    assert agent.check([t.model_copy(update={"minimum": D(5)})])  # Minimum and Transit read swapped


def _swap_lead(text: str) -> str:
    """Every rate row with its transit and minimum in the other order."""
    return re.sub(r"(?m)^([A-Z]{3}-[A-Z]{3}\s+)(\d+)(\s+)(\S+ [\d.]+)", r"\1\4\3\2", text)


@pytest.mark.parametrize(
    "change",
    [
        # a note with both words and no digit, over a header that names neither
        lambda s, h: s.replace(h, "Lane        Days        Min charge", 1).replace(
            "\n\n" + "Lane",
            "\nNote: minimum charges apply before surcharges; transit times are calendar days.\n\nLane",
            1,
        ),
        # a legend line under the header, naming the columns in the other order
        lambda s, h: re.sub(
            r"(?m)^(Lane .*\n)", r"\1(minimum per shipment in EUR, transit in calendar days)\n", s, count=1
        ),
        # the header's names one line above its breaks, minimum first
        lambda s, h: _swap_lead(s.replace(h + "   ", "Lane        Minimum     Transit\n", 1)),
    ],
    ids=["note", "legend", "names_above_breaks"],
)
def test_transit_and_minimum_come_from_the_header_only(start_kit, change):
    source = (start_kit / "yellowtail-air-freight-tariff-2026-11.txt").read_text()
    want = {(t.origin, t.destination): (t.transit_days, t.minimum) for t in agent.parse("y.txt", source.encode())}
    tariffs = agent.parse("y.txt", change(source, "Lane        Transit     Minimum").encode())
    assert {(t.origin, t.destination): (t.transit_days, t.minimum) for t in tariffs} == want
    assert agent.check(tariffs) == []


def test_unknown_layout_is_refused():
    with pytest.raises(ValueError):
        agent.parse("x.txt", b"Some Air | Air export tariff\nCurrency EUR\nFRA-JFK 3 100,00 1,00 2,00\n")


async def test_folder_reads_the_scan_through_the_model_once(ctx, start_kit, monkeypatch):
    pages = json.loads((FIXTURES / "danube-model-pages.json").read_text())
    calls = []

    async def fake_ask_json(ctx, **kw):
        calls.append(kw["images"])
        return LlmResult(data=pages[len(calls) - 1], model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

    monkeypatch.setattr(llm, "ask_json", fake_ask_json)
    await agent.ingest_folder(ctx, start_kit)
    assert len(calls) == 2
    assert [kind for kind, _ in ctx.emitted].count("tariff.ingested") == 10
    assert "tariff.failed" not in [kind for kind, _ in ctx.emitted]
    lane = pages[0]["lanes"][0]
    found = store.lookup(ctx, lane["origin"], lane["destination"], date(2026, 11, 21))
    assert pages[0]["carrier"] in {t.carrier for t in found}
    assert store.carriers(ctx, "airline", lane["destination"])
    zone = ctx.db.execute("SELECT zone, airport FROM pickup_zones LIMIT 1").fetchone()
    assert store.airport_of(ctx, zone["zone"]) == zone["airport"]

    await agent.ingest_folder(ctx, start_kit)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "words", ["one hundred and twenty boxes", "one thousand twenty pieces", "hundert und zwölf Kisten"]
)
def test_a_spoken_compound_number_is_left_whole(words):
    assert agent._spoken([f"We ship {words}, 5 kg each."]) == [f"We ship {words}, 5 kg each."]  # never "... 20 boxes"


def test_an_html_attachment_keeps_its_words_apart_and_reads_only_what_shows():
    page = (b"<html><body><p>Pickup in <b>50978</b> Cologne, gross weight <b>480</b> kg</p><div hidden>900 kg</div>"
            b"<template><p>Total 70.00 EUR</p></template><noscript>Total 80.00 EUR</noscript></body></html>")  # fmt: skip
    assert agent._lines("request.html", page) == ["Pickup in 50978 Cologne, gross weight 480 kg"]


def zipped(*members: tuple[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return out.getvalue()


def test_a_zip_inside_a_zip_is_never_opened_and_a_large_archive_not_read():
    renamed = zipped(("note.txt", b"Gross weight 300 kg"), ("inner.dat", zipped(("rates.txt", b"Total 999.00 EUR"))))
    assert agent._lines("offer.zip", renamed) == ["Gross weight 300 kg"]
    with pytest.raises(agent.Unreadable):  # 64 MB from about 64 kB
        agent._lines("offer.zip", zipped(*((f"part{i}.txt", b"0" * 1_000_000) for i in range(64))))
