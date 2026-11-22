import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pixel.contracts import RateOption, Rfq
from pixel.pricing import engine, selection

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "scored-rounds.json").read_text())
T0 = datetime(2026, 11, 21, 15, tzinfo=UTC)


def _round(r: dict) -> tuple[Rfq, list[RateOption]]:
    rfq = Rfq(session_id=r["round"], message_id=r["round"], customer="probe@hackloop.mail", received_at=T0,
              deadline_at=T0, **{k: v for k, v in r["rfq"].items() if v is not None})  # fmt: skip
    return rfq, [RateOption(**dict(zip(FIXTURE["fields"], o, strict=True))) for o in r["options"]]


@pytest.mark.parametrize("r", FIXTURE["rounds"], ids=lambda r: f"round {r['round']}")
def test_the_options_of_a_scored_round_give_the_scorers_best(r):
    """Every scored round of 21.11. with the options it had when we quoted: today's pickup choice and selection
    give the scorer's best total, airline and trucker, in any order of the options."""
    rfq, options = _round(r)
    for ordered in (options, options[::-1]):
        pick = selection.select(rfq, engine.combine(rfq, ordered))
        assert (str(pick.total_eur), pick.carrier, pick.trucker) == tuple(r["scorer"].values())
