"""Generalization: the fixtures in `tests/fixtures/` hold across tariff layouts and request phrasings.

The checks live in `tools/benchmark_generalization.py`, which prints the same results for people; these tests fail
on what it reports as FAIL. Counts come from the fixture files, never from this file.
"""

import asyncio
import json
from decimal import Decimal as D

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.contracts import LlmResult
from pixel.core import events
from tools import benchmark_generalization as bench
from tools import simulate


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


def test_an_attachment_in_another_container_is_read_like_the_original_or_not_at_all():
    """A captured attachment as html, a forwarded mail, a zip or other text: never read into other figures."""
    reads = bench.audit_containers()
    assert reads
    assert [f"{c.change}: {c.fixture} {c.detail}" for c in reads if c.outcome == "wrong"] == []


def test_a_changed_tracking_question_finds_its_shipment_or_asks_never_another():
    """Every live tracking question finds its shipment as sent; changed one way, it may ask for the reference."""
    reads = bench.audit_tracking()
    assert reads
    assert [f"{r.change}: {r.case_id} {r.outcome}" for r in reads
            if r.outcome == "wrong" or (r.change == "as sent" and r.outcome != "right")] == []  # fmt: skip


def test_an_answer_naming_another_airport_changes_no_field_of_the_request():
    """Round 69: the answer to our question named an airport that became the origin. Outside the known gaps every
    answer leaves the request as it was, or the model reads it; a known gap that reads right fails too."""
    reads = bench.audit_followups()
    assert reads
    assert [f"{f.answer}: {f.fixture} {f.detail}" for f in reads if f.outcome == "wrong" and not f.known_gap] == []
    assert [f"{f.answer}: {f.fixture}" for f in reads if f.known_gap and f.outcome != "wrong"] == []


def test_the_rate_desk_answer_in_an_attachment_gives_the_same_rates():
    """The desk never goes to the model: its rates in a file it writes as lines or as a table must be read."""
    reads = bench.audit_desk_attachments()
    assert reads
    assert [f"{d.layout}, {d.fmt}: {d.detail}" for d in reads if d.outcome != "right"] == []


def test_spreadsheet_layout_changes_parse_the_same_or_are_refused():
    changes = bench.audit_xlsx_changes()
    assert any(v.outcome == "parsed right" for v in changes)
    assert [f"{v.file}: {v.outcome} {v.detail}" for v in changes if not v.safe and not v.known_gap] == []


def test_the_rules_read_every_captured_request_as_a_person_did():
    """`rfqs/live.jsonl` holds what a person read from each captured request. A wrong rules reading fails unless
    its row names it as a known gap. A request without a finished row is a to-do the benchmark prints (`--stub`),
    never a failure: capturing a mail must not stop the host's deploy job."""
    live = bench.audit_live()
    assert any(r.right for r in live)
    assert [f"{r.fixture}: {r.wrong_fields}" for r in live if r.right is False and not r.known_gap] == []
    assert all(r.judged for r in live if not r.todo)


def test_a_missing_or_unfinished_row_is_a_to_do_and_a_misspelled_field_fails(tmp_path, monkeypatch):
    mails, rfqs = tmp_path / "mails", tmp_path / "rfqs"
    mails.mkdir(), rfqs.mkdir()
    raw = (bench.MAILS / "rfq-port-to-port.json").read_text()
    for name in ("rfq-a.json", "rfq-b.json", "rfq-c.json"):
        (mails / name).write_text(raw)
    rows = [{"fixture": "rfq-b.json", "expected": {"weight_kg": "?", "destination": "JFK"}},
            {"fixture": "rfq-c.json", "expected": {"weight": "1540"}}]  # fmt: skip
    (rfqs / "live.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(bench, "MAILS", mails)
    monkeypatch.setattr(bench, "RFQ_FIXTURES", rfqs)
    a, b, c = bench.audit_live()
    assert a.todo and a.right is None and b.todo and b.right is None
    assert c.right is False and c.wrong_fields == ["unknown field weight"]
    assert bench.live_by_field([a, b, c]) == {}  # nothing was compared


def test_a_mutation_named_as_a_known_gap_is_reported_not_failed(tmp_path, monkeypatch):
    mails, rfqs = tmp_path / "mails", tmp_path / "rfqs"
    mails.mkdir(), rfqs.mkdir()
    (mails / "rfq-a.json").write_text((bench.MAILS / "rfq-port-to-port.json").read_text())
    row = {"fixture": "rfq-a.json", "expected": {"weight_kg": "1"}, "known_gap_mutations": {"reply_subject": "x"}}
    (rfqs / "live.jsonl").write_text(json.dumps(row))
    monkeypatch.setattr(bench, "MAILS", mails)
    monkeypatch.setattr(bench, "RFQ_FIXTURES", rfqs)
    found = {r.fixture.split()[-1]: r for r in bench.audit_live_mutations()}
    assert found["reply_subject"].right is False and found["reply_subject"].known_gap == "x"
    assert found["quoted_old_request"].right is False and found["quoted_old_request"].known_gap is None


def test_the_pounds_change_rewrites_only_the_shipments_figure():
    raw = {"subject": "s", "body": "855 kg gross, the truck takes 1855 kg"}
    body = bench._mutations(raw, {"weight_kg": "855"})["pounds"][0]["body"]
    assert body == "1885 lbs gross, the truck takes 1855 kg"


def test_captured_requests_changed_as_seen_live_are_read_right_or_left_to_the_model():
    mutated = bench.audit_live_mutations()
    assert mutated
    assert [f"{r.fixture}: {r.wrong_fields}" for r in mutated if r.right is False and not r.known_gap] == []


def test_a_mail_that_contradicts_its_attached_document_is_read_by_the_document():
    """Rounds 33 to 35: every figure the attached document states, changed in the mail, in every column order,
    format and layout. A reading other than the document's fails unless `document-conflicts.json` names the
    changed field (or field:layout) as a known gap."""
    results = bench.document_conflicts.audit()
    assert len(results) > 1000
    assert any(r.right for r in results)
    assert [f"{r.name}: {r.wrong_fields}" for r in results if r.right is False and not r.known_gap] == []


def test_a_cross_check_that_reads_the_mails_figures_never_beats_the_document():
    """Rounds 82 and 130: gpt-4.1 read the mail's figure and the cross-check took it over the document's."""
    results = bench.document_conflicts.audit(adversarial=True)
    assert [f"{r.name}: {r.wrong_fields}" for r in results if r.right is False and not r.known_gap] == []


def test_generated_requests_read_no_field_wrong_outside_the_known_gaps():
    """Every seventh document case, every mail case and every eleventh carrier answer of `tools/simulate.py`: read
    right or left to the model, unless `rfqs/simulate-known-gaps.json` names the class; a gap that reads right now
    must lose its note."""
    cases = [*simulate.document_cases(step=7), *simulate.mail_cases(), *simulate.bid_cases(step=11)]
    outcomes = asyncio.run(simulate.run(cases))
    assert [f"{o.case.family} {o.case.axes}: {o.wrong}" for o in outcomes if o.verdict == "wrong" and not o.gap] == []
    assert [
        o.case.axes for o in outcomes if o.case.family == "mail" and o.verdict == "model"
    ] == []  # rules read every unit
    assert simulate.stale_gaps(outcomes) == []


def test_every_kind_and_channel_has_a_case_or_a_known_gap():
    """Rounds 63 to 67 were lost to carrier answers in attachments, a channel no fixture or generated case had."""
    assert bench.print_matrix(bench.channel_coverage(), bench.channel_gaps()) == []


def test_every_later_mail_in_a_session_is_read_and_acted_on_right():
    from tools import followup_matrix

    reads = followup_matrix.audit()
    assert len(reads) > 150
    assert [(r.case_id, r.got) for r in reads if not r.ok and not r.known_gap] == []
    assert [r.case_id for r in reads if r.ok and r.known_gap] == []  # a closed gap leaves the fixture
