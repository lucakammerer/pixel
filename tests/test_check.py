"""The last check before a quote leaves: one case per way a round was lost, and the quotes it must let pass."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_outbox import ingest_rfq, kinds, make_ctx

from pixel.agents.rfq import agent, check, compose, dangerous, extract
from pixel.agents.rfq.extract import DG_DOCUMENTS
from pixel.api import mailbox
from pixel.contracts import Preference, Quote, RateOption, Rfq, Source
from pixel.core import events, orchestrator, poller
from pixel.pricing import calc, store

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
MAILS = Path(__file__).parent / "fixtures" / "mails"
PLAIN = "Could you quote the cheapest air freight from FRA to ORD, 679 kg, 4.19 cbm? Thanks, Anna"


def rfq(door: bool = False, weight: str = "679", volume: str = "4.19", **kw) -> Rfq:
    fields = {"session_id": 1, "message_id": 1, "customer": "buyer@example.com", "origin": "FRA", "destination": "ORD",
                  "weight_kg": Decimal(weight), "volume_cbm": Decimal(volume), "door_pickup": door,
                  "pickup_postcode": "51896" if door else None, "pickup_zone": "51" if door else None,
                  "received_at": NOW, "deadline_at": NOW} | kw  # fmt: skip
    return Rfq(**fields)


def airline(total: str, carrier: str = "Xenon Cargo Airlines", days: int = 3, kg: str = "699.73") -> RateOption:
    return RateOption(source=Source.PORTAL, carrier=carrier, origin="FRA", destination="ORD",
                      chargeable_kg=Decimal(kg), billed_kg=Decimal(kg), freight=Decimal(total), total=Decimal(total),
                      currency="EUR", total_eur=Decimal(total), transit_days=days, rate_id="R-1")  # fmt: skip


def truck(total: str, trucker: str, hours: int = 24) -> RateOption:
    return RateOption(source=Source.BID, carrier=trucker, origin="FRA", destination="ORD",
                      chargeable_kg=Decimal("699.73"), billed_kg=Decimal("699.73"), freight=Decimal(0),
                      pickup=Decimal(total), trucker=trucker, pickup_hours=hours, total=Decimal(total),
                      currency="EUR", total_eur=Decimal(total), transit_days=0)  # fmt: skip


def quote(request: Rfq, option: RateOption, body: str | None = None) -> Quote:
    subject, text = compose.render(request, option, None, "Air freight quote", "Lena Berger", PLAIN)
    return Quote(id="Q-1", session_id=request.session_id, rfq=request, option=option, total=option.total, currency=option.currency,
                 subject=subject, body=text if body is None else body, created_at=NOW)  # fmt: skip


def rules(findings, level=None):
    return {f.rule for f in findings if level is None or f.level == level}


# --- quotes that must pass ------------------------------------------------------------------------------------------


def test_a_correct_port_to_port_quote_has_no_finding():
    option = airline("1483.43")
    assert check.run(quote(rfq(), option), PLAIN, [option, airline("1728.33", "Aurelia")]) == []


def test_a_correct_door_to_port_quote_passes():
    """Round 4: Xenon plus the cheapest trucker, Elbkante."""
    found = [airline("1483.43"), truck("115.98", "Elbkante"), truck("187.97", "Westfalenroute")]
    option = calc.with_pickup(found[0], Decimal("115.98"), "Elbkante")
    assert check.blocking(check.run(quote(rfq(door=True), option), PLAIN, found)) is False


def test_a_german_quote_passes():
    option = airline("1483.43")
    request = rfq(language="de")
    _subject, body = compose.render(request, option, None, "Luftfrachtanfrage", "Lena Berger", "Bitte um Angebot")
    q = quote(request, option, body)
    assert check.run(q, "Bitte um ein Angebot FRA nach ORD, 679 kg", [option]) == []


# --- the rounds we lost ---------------------------------------------------------------------------------------------


def test_round_9_lots_added_up_wrong_block():
    mail = json.loads((MAILS / "rfq-door-three-lots.json").read_text())["body"]
    assert check.lot_weight(mail) == Decimal(1952)
    option = airline("4340.00", kg="1750")
    wrong = check.run(quote(rfq(weight="1750", volume="3.36"), option), mail, [option])
    assert "weight" in rules(wrong, check.BLOCK)
    option = airline("4840.96", kg="1952")
    right = check.run(quote(rfq(weight="1952", volume="4.68"), option), mail, [option])
    assert "weight" not in rules(right)


@pytest.mark.parametrize("name, weight", [("rfq-lots-two", 998), ("rfq-lots-gross-after", 486)])
def test_lot_weight_agrees_with_the_captured_lot_requests(name, weight):
    assert check.lot_weight(json.loads((MAILS / f"{name}.json").read_text())["body"]) == weight


def test_a_lot_line_that_does_not_read_gives_no_sum():
    text = "Lot 1: 4 drums, together 730 kg gross\nLot 2: see the attached list\nLot 3: 5 cases, each 206 kg gross"
    assert check.lot_weight(text) is None


def test_lot_weight_each_after_the_figure_and_net_first():
    text = "Lot 1: 3 cartons, 41 kg net, 48 kg gross each\nLot 2: 2 pallets, together 300 kg gross"
    assert check.lot_weight(text) == Decimal(444)


@pytest.mark.parametrize(
    "text, kg",
    [
        ("Lot 1: 2 x 300 kg\nLot 2: 1 x 200 kg", 800),
        ("Lot 1: 3 cartons à 30 kg\nLot 2: 2 pallets, together 500 kg gross", 590),
        ("Item 1: 2 pallets 400kg each\nItem 2: 1 pallet 250kg", 1050),
    ],
)
def test_lot_weight_reads_factors_written_next_to_the_figure(text, kg):
    assert check.lot_weight(text) == Decimal(kg)


def test_a_long_lot_line_gives_no_sum():
    assert check.lot_weight("Lot 1: 2 x 300 kg " + "9" * 5000 + "\nLot 2: 1 x 200 kg") is None


def test_the_customers_subject_is_not_a_template_leftover():
    option = airline("1599.41")
    q = quote(rfq(), option).model_copy(update={"subject": "Re: DG: None [[urgent]]"})
    assert check.run(q, PLAIN, [option]) == []


def test_round_10_dangerous_goods_without_the_papers_block():
    option = airline("2609.75")
    marked = rfq(missing=(DG_DOCUMENTS,))
    assert "dangerous_goods" in rules(check.run(quote(marked, option), PLAIN, [option]), check.BLOCK)
    un = "Please quote 715 kg of adhesives, UN1133, class 3, to ORD."
    assert "dangerous_goods" in rules(check.run(quote(rfq(), option), un, [option]), check.WARN)


@pytest.mark.parametrize(
    "text",
    [
        "not classified as dangerous goods",
        "Dangerous goods: not applicable",
        "without lithium batteries",
        "Packed in class 2 export cartons",
        "Invoice UN2026",
    ],
)
def test_dg_words_the_rfq_did_not_mark_never_block(text):
    option = airline("2609.75")
    assert not check.blocking(check.run(quote(rfq(), option), text, [option]))


def test_amounts_in_the_dangerous_goods_note_are_not_a_second_price():
    option = airline("1599.41")
    q = quote(rfq(missing=(DG_DOCUMENTS,)), option)
    goods = "1 | UN3481 Li-ion batteries | 12 kg | 450.00 EUR | 1599.41 EUR"
    q = q.model_copy(update={"body": dangerous.with_note(q.body, goods, "en", "PL.xlsx")})
    assert check.run(q, PLAIN, [option]) == []


def test_dangerous_goods_with_the_papers_asked_for_pass():
    option = airline("2609.75")
    q = quote(rfq(missing=(DG_DOCUMENTS,)), option)
    q = q.model_copy(update={"body": dangerous.with_note(q.body, "UN1133", "en", "PL.xlsx")})
    assert "dangerous_goods" not in rules(check.run(q, "UN1133 adhesives", [option]))


def test_non_dangerous_adhesives_only_warn():
    option = airline("2609.75")
    text = "3 pieces of industrial adhesives, 679 kg, general cargo, non-dangerous."
    found = check.run(quote(rfq(), option), text, [option], ("PL_53839.xlsx",))
    assert rules(found, check.WARN) == {"dangerous_goods"} and not check.blocking(found)


def test_round_15_pickup_missing_blocks():
    option = airline("1058.20")
    assert "pickup" in rules(check.run(quote(rfq(door=True), option), PLAIN, [option]), check.BLOCK)


def test_pickup_on_a_port_to_port_request_blocks():
    option = calc.with_pickup(airline("1058.20"), Decimal("90.60"), "Elbkante")
    assert "pickup" in rules(check.run(quote(rfq(), option), PLAIN, [option]), check.BLOCK)


def test_round_16_a_dear_trucker_more_than_5_percent_above_warns():
    found = [airline("1490.98", "Helix", 2), airline("1378.75", "Nova Atlas", 4), truck("86.06", "Vogelsberg", 29),
             truck("197.12", "Bodenseefracht", 6)]  # fmt: skip
    option = calc.with_pickup(found[0], Decimal("197.12"), "Bodenseefracht")
    findings = check.run(quote(rfq(door=True, preference=Preference.FASTEST), option), PLAIN, found)
    assert "best_price" in rules(findings, check.WARN)
    cheap = calc.with_pickup(found[0], Decimal("86.06"), "Vogelsberg")
    assert "best_price" not in rules(
        check.run(quote(rfq(door=True, preference=Preference.FASTEST), cheap), PLAIN, found)
    )


# --- the text the scorer reads --------------------------------------------------------------------------------------


def test_a_second_amount_in_the_mail_blocks():
    option = airline("1599.41")
    q = quote(rfq(), option)
    q = q.model_copy(update={"body": q.body + "\n\nThe airline alone charges EUR 1483.43."})
    assert "text_amounts" in rules(check.run(q, PLAIN, [option]), check.BLOCK)


def test_each_further_shipment_and_their_sum_are_no_second_amount():
    """Round 125: two shipments in one mail, each quoted with the sum of both; only another amount still blocks."""
    request = "Fastest from MUC, each quoted:\n1. To SGN, 6 pieces, 1592 kg, 6.77 cbm.\n2. To ICN, 7 pieces, 813 kg, 6.04 cbm."
    option = airline("5810.80")
    q = quote(rfq(), option)
    shipments = "For the shipment to ICN I would go with Ibisair Cargo: 3439.60 EUR all-in."
    sent = q.model_copy(update={"body": q.body + f"\n\n{shipments} All shipments together: 9250.40 EUR all-in."})
    assert "text_amounts" not in rules(check.run(sent, request, [option]))
    wrong = q.model_copy(update={"body": q.body + f"\n\n{shipments} All shipments together: 9999.00 EUR all-in."})
    assert "text_amounts" in rules(check.run(wrong, request, [option]), check.BLOCK)


def test_the_total_twice_or_missing_blocks():
    option = airline("1599.41")
    q = quote(rfq(), option)
    assert "text_total" in rules(check.run(q.model_copy(update={"body": q.body + " 1599.41"}), PLAIN, [option]))
    assert "text_total" in rules(check.run(q.model_copy(update={"body": "Hello"}), PLAIN, [option]))


def test_a_template_leftover_blocks():
    option = airline("1599.41")
    q = quote(rfq(), option)
    assert "text_leftover" in rules(check.run(q.model_copy(update={"body": q.body + " {first_name}"}), PLAIN, [option]))


def test_parts_that_do_not_add_up_block():
    option = airline("1599.41").model_copy(update={"fuel": Decimal(50)})
    assert "total" in rules(check.run(quote(rfq(), option), PLAIN, [option]), check.BLOCK)


def test_a_chargeable_weight_below_the_volume_weight_blocks():
    option = airline("1483.43", kg="679")
    assert "chargeable" in rules(check.run(quote(rfq(), option), PLAIN, [option]), check.BLOCK)


def test_a_lane_other_than_the_request_blocks():
    option = airline("1483.43").model_copy(update={"origin": "CGN"})
    assert "lane" in rules(check.run(quote(rfq(), option), PLAIN, [option]), check.BLOCK)


def test_a_broken_rule_is_a_warning_never_an_exception(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(check, "RULES", (broken,))
    assert [f.level for f in check.run(quote(rfq(), airline("1483.43")), PLAIN)] == [check.WARN]


# --- every drafted quote is checked --------------------------------------------------------------------------------


def _session(ctx, request: Rfq, found: list[RateOption]) -> None:
    events.emit(ctx, "rfq.extracted", "rfq", session_id=request.session_id, payload=request.model_dump(mode="json"))
    for o in found:
        events.emit(ctx, "option.found", "pricing", session_id=request.session_id,
                    payload={"option": o.model_dump(mode="json")})  # fmt: skip


def _checked(conn) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events WHERE kind = 'quote.checked'")]


def test_a_drafted_quote_is_checked_and_a_block_is_an_issue_for_the_alert(conn):
    from pixel.core import feedback

    ctx = make_ctx(conn)
    mail = ingest_rfq(ctx)
    request = rfq(door=True, session_id=mail.session_id, message_id=mail.id, customer=mail.sender)
    _session(ctx, request, [airline("1058.20")])
    q = agent._quote(ctx, request, [airline("1058.20")], None, fallback=True)
    assert q.total == Decimal("1058.20")  # the check never changes the quote
    [checked] = _checked(conn)
    assert checked["blocking"] and checked["fallback"] and checked["findings"][0]["rule"] == "pickup"
    assert [i["issue"] for i in feedback.attention(conn, 5)] == ["quote_doubtful"]


def test_a_clean_quote_is_logged_without_an_issue(conn):
    from pixel.core import feedback

    ctx = make_ctx(conn)
    mail = ingest_rfq(ctx)
    # the mail's own figures: 676 kg and 5.79 cbm, chargeable 966.93 kg
    request = rfq(weight="676", volume="5.79", session_id=mail.session_id, message_id=mail.id, customer=mail.sender)
    _session(ctx, request, [airline("1483.43", kg="966.93")])
    agent._quote(ctx, request, [airline("1483.43", kg="966.93")], None, fallback=False)
    [checked] = _checked(conn)
    assert not checked["blocking"]
    assert feedback.attention(conn, 5) == []


async def test_corroborated_packing_list_weight_passes_the_quote_check(conn, monkeypatch):
    ctx = make_ctx(conn, dry_run=True)
    raw = json.loads((MAILS / "rfq-door-packing-weight-differs-from-mail.json").read_text())
    mail = poller.ingest(ctx, mailbox.to_message(raw, "in"))
    zone = extract._pickup(extract._text(mail))[1][:2]
    store.save_zones(ctx, [{"zone": zone, "area": "Pickup area", "airport": "AAA"}])
    packing = (MAILS.parent / "rfqs" / "round-57-packing-list.txt").read_text().splitlines()

    async def attachment_lines(ctx, message):
        # Reconstruct the corroborated figures from the live event. The mail capture holds only file metadata.
        return [(message.attachments[0].filename, packing)]

    monkeypatch.setattr(extract, "_attachments", attachment_lines)
    deadline = orchestrator.Deadline(NOW, NOW, NOW, NOW)
    request = await extract.extract(ctx, mail, deadline)
    assert request.weight_kg == extract._document_facts("\n".join(packing))["weight_kg"]
    kg = calc.chargeable_kg(request.weight_kg, request.volume_cbm)
    air = airline("5000.00", kg=str(kg)).model_copy(
        update={"origin": request.origin, "destination": request.destination}
    )
    option = calc.with_pickup(air, Decimal("500.00"), "Trucker")
    drafted = agent._quote(ctx, request, [option], None, fallback=False)
    [checked] = _checked(conn)
    assert not checked["blocking"], checked["findings"]

    wrong = request.model_copy(update={"weight_kg": request.weight_kg - 1})
    agent._check(ctx, wrong, drafted.model_copy(update={"rfq": wrong}))
    assert any(f["rule"] == "weight_unexplained" for f in _checked(conn)[-1]["findings"])


def test_precheck_off_skips_the_check(conn):
    ctx = make_ctx(conn, precheck="off")
    mail = ingest_rfq(ctx)
    request = rfq(session_id=mail.session_id, message_id=mail.id, customer=mail.sender)
    agent._quote(ctx, request, [airline("1483.43")], None, fallback=False)
    assert "quote.checked" not in kinds(ctx)


def test_a_check_that_fails_is_an_error_event_never_a_lost_quote(conn, monkeypatch):
    ctx = make_ctx(conn)
    mail = ingest_rfq(ctx)
    request = rfq(session_id=mail.session_id, message_id=mail.id, customer=mail.sender)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(check, "run", broken)
    assert agent._quote(ctx, request, [airline("1483.43")], None, fallback=False).total == Decimal("1483.43")
    assert "error" in kinds(ctx) and "quote.drafted" in kinds(ctx)


# --- weight accounting: the priced weight must be one the mail's own figures explain ---------------------------------


def accounting(text: str, weight: str, files: tuple[str, ...] = ()) -> list[str]:
    option = airline("2000.00", kg=str(max(Decimal(weight), Decimal("699.73"))))
    return [f.level for f in check.run(quote(rfq(weight=weight), option), text, [option], files)
            if f.rule == "weight_unexplained"]  # fmt: skip


@pytest.mark.parametrize(
    "text, misread, right",
    [
        ("FRA to ORD, gross weight: 480 lbs, 2.1 cbm", "480", "217.72"),  # 21.11. 18:25: pounds read as kg
        ("FRA to ORD, 4 cartons, each 48 kg gross (41 kg net), 0.6 cbm", "48", "192"),  # per piece as the total
        ("FRA to ORD, 3 pallets à 250 kg, 2.4 cbm", "250", "750"),
        ("FRA to ORD, 1,5 t, 6 cbm", "1.5", "1500"),
        ("FRA to ORD\nLot 1: 2 pallets, 900 kg, 2 cbm\nLot 2: one crate, 922 kg, 1 cbm", "922", "1822"),  # #18
        ("FRA to ORD, 715 kg gross (680 kg net), 3 cbm", "680", "715"),  # the net weight priced
    ],
)
def test_a_weight_the_mails_figures_do_not_give_blocks(text, misread, right):
    assert accounting(text, misread) == [check.BLOCK]
    assert accounting(text, right) == []


@pytest.mark.parametrize(
    "text",
    [
        "Last time we shipped 800 kg with you. This time FRA to ORD, 450 kg, 2 cbm, cheapest.",
        "FRA to ORD, 450 kg, 2 cbm, cheapest. Our truck can carry 3 t, so one trip is enough.",
        "FRA to ORD, 2 pallets, 450 kg in total, each pallet rated for 1000 kg, 2 cbm.",
    ],
)
def test_a_heavier_figure_that_is_not_this_shipment_does_not_block(text):
    assert accounting(text, "450") == []


def test_round_9_a_lot_left_out_or_one_lot_alone_blocks():
    mail = json.loads((MAILS / "rfq-door-three-lots.json").read_text())["body"]
    for wrong in ("1750", "1030", "730"):
        assert accounting(mail, wrong) == [check.BLOCK], wrong
    assert accounting(mail, "1952") == []


@pytest.mark.parametrize(
    "text, weight",
    [
        ("FRA to ORD, 4 pallets, each 180 kg, total 720 kg, 3 cbm", "720"),
        ("FRA to ORD, 700 kg, max 1000 kg per pallet, 3 cbm", "700"),
        ("FRA to ORD, 2 x 300 kg, 2 cbm", "600"),
        ("FRA to ORD, gross weight: 1.234 kg, 4 cbm", "1234"),
        ("FRA to ORD, Gewicht: 480, 2 cbm", "480"),
        ("FRA to ORD, 218 kg (480 lbs), 1 cbm", "218"),
        ("FRA to ORD\n- 2 pallets, 380 kg\n- 1 crate, 618 kg\nTotal: 998 kg", "998"),
    ],
)
def test_a_weight_the_figures_give_passes(text, weight):
    assert accounting(text, weight) == []


@pytest.mark.parametrize("name", sorted(p.stem for p in MAILS.glob("rfq-*.json")))
def test_every_captured_request_read_by_the_rules_is_explained(name):
    data = json.loads((MAILS / f"{name}.json").read_text())
    message = mailbox.to_message(data, "in")
    read = extract.by_rules(message, orchestrator.Deadline(NOW, NOW, NOW, NOW))
    if read is None:
        pytest.skip("the rules do not read it")
    assert accounting(extract._text(message), str(read.weight_kg)) == []


def test_a_mail_without_any_weight_only_warns():
    assert accounting("FRA to ORD, details in the packing list", "500", ("packing.pdf",)) == [check.WARN]
    assert accounting("", "500") == [check.WARN]


def _written_weights() -> list[tuple[str, str, str]]:
    """Every hand-written request with the gross weight its author says it states (fixtures README: not generated
    from the code under review)."""
    fixtures, cases = Path(__file__).parent / "fixtures", []
    for name in ("rfqs/rewrites.jsonl", "rfqs/partial_cases.jsonl"):
        for line in (fixtures / name).read_text().splitlines():
            row = json.loads(line)
            mail = row.get("message") or {}
            text = row.get("text") or f"{mail.get('subject', '')}\n\n{mail.get('body', '')}"
            if weight := (row.get("expected") or {}).get("weight_kg"):
                cases.append((row["case_id"], text, weight))
    variants = json.loads((fixtures / "request-variants.json").read_text())
    for v in variants["variants"]:
        weight = (v.get("expect") or {}).get("weight_kg", variants["base"]["weight_kg"])
        cases.append((f"variant-{v['name']}", f"{v.get('subject', '')}\n\n{v['body']}", weight))
    return cases


@pytest.mark.parametrize("case, text, weight", _written_weights(), ids=[c[0] for c in _written_weights()])
def test_no_hand_written_request_with_its_true_weight_is_blocked(case, text, weight):
    assert check.BLOCK not in accounting(text, weight)
