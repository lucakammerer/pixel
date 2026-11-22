import asyncio
from decimal import Decimal
from pathlib import Path

from tools import capture, replay


def test_round_166_accepted():
    """Session 50140, round 166: accepted. Captured with tools.capture."""
    path = Path(__file__).parent / "fixtures" / "sessions" / "round-166-accepted.json"
    quote = capture.quoted(asyncio.run(replay.fixture(path)))
    assert quote is not None, "no quote left"
    assert quote["lane"] == ("DUS", "YYZ")
    assert abs(quote["total_eur"] - Decimal("2913.96")) <= Decimal("2913.96") * Decimal("0.05")
    assert (quote["airline"], quote["trucker"]) == ("Aurelia Air Cargo", "Schwabenbote Transporte")
    assert quote["language"] == "en"


def test_round_186_turkish_fastest():
    """Session 56183, round 186: not_what_was_asked. "en hızlı seçeneği istiyoruz" was read as cheapest and Aurelia
    (3 days) quoted; the fastest is Fjordline (2 days)."""
    path = Path(__file__).parent / "fixtures" / "sessions" / "round-186-turkish-fastest.json"
    quote = capture.quoted(asyncio.run(replay.fixture(path)))
    assert quote is not None, "no quote left"
    assert quote["lane"] == ("FRA", "TLV") and quote["language"] == "tr"
    assert quote["airline"] == "Fjordline Air Cargo"
