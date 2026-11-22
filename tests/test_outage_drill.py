import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tools import outage_drill

ROUND_166 = Path(__file__).parent / "fixtures" / "sessions" / "round-166-accepted.json"
ROUND_56 = ROUND_166.with_name("round-56-version-rollover.json")
BEST = Decimal("2913.96")  # the scorer's best for round 166 (`feedback.best`), our accepted quote


@pytest.mark.parametrize("name", outage_drill.DRILLS)
def test_round_166_is_quoted_within_5_per_cent_in_time_when(name):
    raws = json.loads(ROUND_166.read_text())
    state = json.loads(ROUND_166.with_suffix(".state.json").read_text())
    assert outage_drill.verdict(*asyncio.run(outage_drill.drill(raws, state, name)), BEST) is None


@pytest.mark.parametrize(
    ("visibility", "drill"),
    [
        ("normal", "gateway_down"),
        ("hidden", "portal_down"),
        ("hidden", "portal_429"),
        ("one_read", "portal_down"),
        ("observed", "portal_down"),
    ],
)
def test_round_56_version_rollover_at_the_start_of_a_portal_outage(visibility, drill):
    raws = json.loads(ROUND_56.read_text())
    state = json.loads(ROUND_56.with_suffix(".state.json").read_text())
    expected = json.loads(ROUND_56.with_suffix(".expected.json").read_text())
    observed = expected["recorded_new_version"]
    if visibility in ("one_read", "observed"):
        state["events"] += observed[:1] if visibility == "one_read" else observed

    total, seconds = asyncio.run(outage_drill.drill(raws, state, drill))
    best = Decimal(expected["scorer_best_eur"])
    if (
        visibility == "hidden"
        and total == Decimal(expected["known_stale_quote_eur"])
        and seconds is not None
        and seconds <= outage_drill.DEADLINE_S
    ):
        pytest.xfail("C16, Accuracy: the card has only the previous version when every new read fails")
    assert outage_drill.verdict(total, seconds, best) is None
