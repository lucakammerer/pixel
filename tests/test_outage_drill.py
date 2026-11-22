import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tools import outage_drill

ROUND_166 = Path(__file__).parent / "fixtures" / "sessions" / "round-166-accepted.json"
BEST = Decimal("2913.96")  # the scorer's best for round 166 (`feedback.best`), our accepted quote


@pytest.mark.parametrize("name", outage_drill.DRILLS)
def test_round_166_is_quoted_within_5_per_cent_in_time_when(name):
    raws = json.loads(ROUND_166.read_text())
    state = json.loads(ROUND_166.with_suffix(".state.json").read_text())
    assert outage_drill.verdict(*asyncio.run(outage_drill.drill(raws, state, name)), BEST) is None
