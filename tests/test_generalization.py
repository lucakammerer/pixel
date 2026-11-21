"""Generalization: the fixtures in `tests/fixtures/` hold across tariff layouts and request phrasings.

The checks live in `tools/benchmark_generalization.py`, which prints the same results for people; these tests fail
on what it reports as FAIL. Counts come from the fixture files, never from this file.
"""

from decimal import Decimal as D

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.contracts import LlmResult
from pixel.core import events
from tools import benchmark_generalization as bench


@pytest.fixture(scope="module")
def kit():
    return bench.load_start_kit()


def test_hand_prices_match_calc(kit):
    report = bench.audit_hand_prices(kit[0])
    assert report.total > 0
    assert report.failures == []


def test_every_start_kit_source_parses_to_its_reviewed_lanes(kit):
    report, lanes = bench.audit_sources(*kit)
    assert report.total > 0
    assert report.failures == []
    assert lanes > 0


def test_rules_extract_rewrites_right_when_they_extract_them():
    """A wrong rules reading fails unless its row names it as a known gap, a handoff to the extractor's owner."""
    rewrites = bench.audit_rewrites()
    assert rewrites
    wrong = [f"{r.case_id}: {r.wrong_fields}" for r in rewrites if r.by_rules and not r.right and not r.known_gap]
    assert wrong == []


async def test_rewrites_the_rules_miss_go_to_the_model_once(monkeypatch):
    """Where the rules give nothing, extract asks the model exactly once with the customer's text."""
    missed = [r.case_id for r in bench.audit_rewrites() if not r.by_rules]
    if not missed:
        pytest.skip("the rules extract every rewrite")
    items = {i["case_id"]: i for i in bench._jsonl(bench.RFQ_FIXTURES / "rewrites.jsonl")}
    monkeypatch.setattr(events, "emit", lambda *a, **kw: None)

    for case_id in missed:
        item = items[case_id]
        exp = item["expected"]
        calls = []

        async def fake_ask(ctx, expected=exp, calls=calls, **kw):
            calls.append(kw)
            fields = ("origin", "destination", "weight_kg", "volume_cbm", "pieces", "preference")
            fields += ("door_pickup", "pickup_postcode", "currency", "language")
            return LlmResult(data={k: expected[k] for k in fields}, model="m", tokens_in=1, tokens_out=1, cost_usd=D(0))

        monkeypatch.setattr(llm, "ask_json", fake_ask)
        rfq = await extract.extract(None, bench._message("Rate request", item["text"]), bench._deadline())
        assert len(calls) == 1, case_id
        assert item["text"] in calls[0]["user"], case_id
        assert (rfq.origin, rfq.destination, rfq.weight_kg) == (exp["origin"], exp["destination"], D(exp["weight_kg"]))


def test_changed_tariff_layouts_are_parsed_right_or_refused():
    """A sheet whose layout changed must never be parsed into wrong prices, unless named as a known gap."""
    variants = bench.audit_variants()
    assert any(v.outcome == "parsed right" for v in variants)
    assert [f"{v.file}: {v.outcome} {v.detail}" for v in variants if not v.safe and not v.known_gap] == []


def test_router_rules_never_misroute_a_probe_mail():
    """A mail the rules place must get its right kind, unless named as a known gap; leaving it to the model is fine."""
    routing = bench.audit_routing()
    assert routing
    assert [f"{r.case_id}: {r.got} ({r.rule})" for r in routing if not r.ok and not r.known_gap] == []


def test_rules_never_guess_on_partial_cases():
    report, _ = bench.audit_partial_cases()
    assert report.total > 0
    assert report.failures == []


def test_carrier_bids_are_read_right_or_refused():
    """A bid the rules read must give the reference total, currency and transit, unless named as a known gap."""
    reads = bench.audit_bids()
    assert reads
    assert [f"{b.case_id}: {b.got}" for b in reads if not b.ok and not b.known_gap] == []


def test_spreadsheet_layout_changes_parse_the_same_or_are_refused():
    changes = bench.audit_xlsx_changes()
    assert any(v.outcome == "parsed right" for v in changes)
    assert [f"{v.file}: {v.outcome} {v.detail}" for v in changes if not v.safe and not v.known_gap] == []


def test_every_captured_request_has_its_reference_and_the_rules_read_it_right():
    """`rfqs/live.jsonl` holds what a person read from each captured request. A wrong rules reading fails unless
    its row names it as a known gap; a request without a row fails until someone writes it (`--stub`)."""
    live = bench.audit_live()
    assert live
    assert [f"{r.fixture}: {r.wrong_fields}" for r in live if r.right is False and not r.known_gap] == []
    assert all(r.judged for r in live)


def test_captured_requests_changed_as_seen_live_are_read_right_or_left_to_the_model():
    mutated = bench.audit_live_mutations()
    assert mutated
    assert [f"{r.fixture}: {r.wrong_fields}" for r in mutated if r.right is False] == []
