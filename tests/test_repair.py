"""Verify, repair, check again (`agents/rfq/repair.py`) and the rules it acts on that the check gained with it."""

import json
from decimal import Decimal

import pytest
from test_check import NOW, PLAIN, airline, quote, rfq, rules
from test_outbox import ingest_rfq, kinds, make_ctx

from pixel.agents.rfq import agent, check, compose, extract, repair
from pixel.contracts import RateOption, Source
from pixel.core import events
from pixel.pricing import session

INSURED = (
    "Please quote FRA to ORD, 679 kg, 4.19 cbm. Goods value EUR 50,000; insure 110% of the value at a premium rate "
    "of 0.3%."
)
CHINESE = "您好，请报价从法兰克福到芝加哥的空运，679公斤，4.19立方米。谢谢！"
FRENCH = (
    "Bonjour, pourriez-vous nous faire une offre pour un envoi de FRA à ORD, 679 kg, 4.19 cbm ? "
    "Nous avons besoin de la solution la moins chère pour cette marchandise. Merci beaucoup et bonne journée."
)


def bid(total: str, carrier: str) -> RateOption:
    return airline(total, carrier).model_copy(update={"source": Source.BID, "rate_id": None})


def _payloads(conn, kind: str) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]


def _session(ctx, request, found):
    events.emit(ctx, "rfq.extracted", "rfq", session_id=request.session_id, payload=request.model_dump(mode="json"))
    for o in found:
        events.emit(ctx, "option.found", "pricing", session_id=request.session_id,
                    payload={"option": o.model_dump(mode="json")})  # fmt: skip


def _request(ctx, **kw):
    """A stored request whose mail states the cargo `rfq` holds (679 kg, 4.19 cbm)."""
    mail = ingest_rfq(ctx)
    _body(ctx.db, mail.id, PLAIN)
    return rfq(session_id=mail.session_id, message_id=mail.id, customer=mail.sender, **kw)


def _body(conn, message_id: int, body: str) -> None:
    row = conn.execute("SELECT data FROM messages WHERE id = ?", (message_id,)).fetchone()
    data = json.loads(row[0]) | {"body": body}
    conn.execute("UPDATE messages SET data = ? WHERE id = ?", (json.dumps(data), message_id))


# --- the new rules --------------------------------------------------------------------------------------------------


def test_an_airline_the_customer_ruled_out_blocks_when_another_is_allowed():
    request = rfq(excluded_carriers=("Xenon Cargo Airlines",))
    xenon, aurelia = airline("1483.43"), airline("1528.33", "Aurelia")
    assert "excluded_carrier" in rules(check.run(quote(request, xenon), PLAIN, [xenon, aurelia]), check.BLOCK)
    alone = check.run(quote(request, xenon), PLAIN, [xenon])
    assert "excluded_carrier" in rules(alone, check.WARN) and not check.blocking(alone)  # R1: an answer beats none


def test_stated_insurance_terms_without_a_premium_block():
    assert extract.insurance(INSURED) is not None
    option = airline("1483.43")
    assert "insurance_missing" in rules(check.run(quote(rfq(), option), INSURED, [option]), check.BLOCK)
    priced = check.run(quote(rfq(), option), INSURED, [option], insurance_eur=Decimal("165.00"))
    assert "insurance_missing" not in rules(priced)


def test_a_chinese_request_answered_in_english_blocks():
    option = airline("1483.43")
    assert "language_sent" in rules(check.run(quote(rfq(language="zh"), option), CHINESE, [option]), check.BLOCK)


def test_a_clear_french_request_answered_in_english_blocks_and_in_french_passes():
    option = airline("1483.43")
    english = quote(rfq(), option)
    assert "language_sent" in rules(check.run(english, FRENCH, [option]), check.BLOCK)
    _subject, body = compose.render(rfq(language="fr"), option, None, "Devis", "Lena Berger", FRENCH)
    assert "language_sent" not in rules(check.run(quote(rfq(language="fr"), option, body), FRENCH, [option]))


def test_an_english_request_with_german_places_raises_no_language_block():
    option = airline("1483.43")
    text = "Hi team, please quote FRA to ORD from our site in Mönchengladbach, 679 kg, 4.19 cbm. Thanks, Anna"
    assert "language_sent" not in rules(check.run(quote(rfq(), option), text, [option]))


def test_every_rendered_quote_has_a_whole_offer_pdf():
    option = airline("1483.43")
    assert "offer_pdf" not in rules(check.run(quote(rfq(), option), PLAIN, [option]))


# --- the loop -------------------------------------------------------------------------------------------------------


async def test_a_distrusted_bid_is_left_out_and_the_fallback_inherits_it(conn):
    """Round 67's kind: a bid read far too low reached the quote; the repair quotes the next airline."""
    ctx = make_ctx(conn)
    request = _request(ctx)
    cheap, good, other = bid("120.00", "Tiny Air"), airline("1483.43"), airline("1528.33", "Aurelia")
    _session(ctx, request, [cheap, good, other])
    sent = await agent._settled(ctx, request, [cheap, good, other], None, fallback=False, translate_by=None,
                                repair_by=None)  # fmt: skip
    assert sent.option.carrier == "Xenon Cargo Airlines" and sent.total == Decimal("1483.43")
    [vetoed] = _payloads(conn, "option.vetoed")
    assert vetoed["name"] == "Tiny Air" and vetoed["rule"] == "implausible_bid"
    assert "Tiny Air" not in {o.carrier for o in session.options(ctx, request.session_id)}
    repaired = _payloads(conn, "quote.repaired")
    assert repaired[0]["kept"] and repaired[0]["total_before"] == "120.00"
    final = _payloads(conn, "quote.checked")[-1]
    assert final["stage"] == "sent" and not final["blocking"] and final["repairs"][0]["rule"] == "implausible_bid"
    fallback = await agent.fallback(ctx, request.session_id)
    assert fallback is not None and fallback.option.carrier != "Tiny Air"


async def test_an_excluded_airline_is_left_out_before_sending(conn):
    ctx = make_ctx(conn)
    request = _request(ctx, excluded_carriers=("Xenon Cargo Airlines",))
    xenon, aurelia = airline("1483.43"), airline("1528.33", "Aurelia")
    _session(ctx, request, [xenon, aurelia])
    drafted = agent._quote(ctx, request, [xenon], None, fallback=False)  # as if select had been given Xenon alone
    settled = await repair.settle(
        ctx, drafted, repair.Draft(request, [xenon, aurelia], None),
        lambda d: _built(ctx, d), lambda q: agent._findings(ctx, q.rfq, q), by=None,
    )  # fmt: skip
    assert settled.option.carrier == "Aurelia"
    assert _payloads(conn, "option.vetoed")[0] == {"kind": "airline", "name": "Xenon Cargo Airlines",
                                                   "rule": "excluded_carrier"}  # fmt: skip


async def _built(ctx, draft):
    return agent._quote(ctx, draft.rfq, draft.options, draft.prose, fallback=False)


async def test_a_french_request_read_as_english_is_answered_in_french(conn):
    """Round 70: French answered in English, rejected as wrong_language. Our French text needs no model."""
    ctx = make_ctx(conn)
    request = _request(ctx)
    _body(conn, request.message_id, FRENCH)
    option = airline("1483.43")
    _session(ctx, request, [option])
    sent = await agent._settled(ctx, request, [option], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.rfq.language == "fr" and "Bonjour" in sent.body
    assert session.rfq(ctx, request.session_id).language == "fr"  # the fallback reads it in French too


async def test_a_repair_that_blocks_on_another_rule_is_not_kept(conn, monkeypatch):
    ctx = make_ctx(conn)
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])

    async def worse(ctx, quote, draft, tools):
        return quote.model_copy(update={"body": quote.body + "\n{placeholder}"}), draft

    monkeypatch.setitem(repair.REPAIRS, "implausible_bid", worse)
    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.total == Decimal("120.00")  # nothing better was found: the quote as it was, and a person hears of it
    assert [p["kept"] for p in _payloads(conn, "quote.repaired")] == [False]
    assert _payloads(conn, "option.vetoed") == []


async def test_a_failing_repair_never_costs_the_quote(conn, monkeypatch):
    ctx = make_ctx(conn)
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])

    async def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setitem(repair.REPAIRS, "implausible_bid", broken)
    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.total == Decimal("120.00") and "error" in kinds(ctx)


async def test_detect_checks_the_text_that_leaves_and_changes_nothing(conn):
    ctx = make_ctx(conn, repair="detect")
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])
    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.total == Decimal("120.00") and "quote.repaired" not in kinds(ctx)
    assert _payloads(conn, "quote.checked")[-1] | {"findings": None} == {
        "quote": sent.id, "stage": "sent", "blocking": True, "fallback": False, "total": "120.00", "repairs": [],
        "findings": None,
    }  # fmt: skip


async def test_no_repair_runs_past_its_time(conn):
    ctx = make_ctx(conn)
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])
    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=NOW)
    assert sent.total == Decimal("120.00") and "quote.repaired" not in kinds(ctx)


@pytest.mark.parametrize("changes", [{"repair": "off"}, {"precheck": "off"}])
async def test_repair_off_and_precheck_off_leave_the_quote_alone(conn, changes):
    ctx = make_ctx(conn, **changes)
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])
    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.total == Decimal("120.00")
    assert not [p for p in _payloads(conn, "quote.checked") if p.get("stage") == "sent"]


def test_a_veto_leaves_out_an_airline_by_source_or_a_trucker():
    bid_ = bid("120.00", "Tiny Air")
    sheet = bid_.model_copy(update={"source": Source.SHEET})
    record = session.veto(bid_, trucker=False)
    assert session.vetoed(bid_, record) and not session.vetoed(sheet, record)
    truck = bid_.model_copy(update={"trucker": "Elbkante", "carrier": "Elbkante"})
    assert session.vetoed(truck, session.veto(truck, trucker=True))
    assert not session.vetoed(sheet, session.veto(truck, trucker=True))


def test_the_draft_of_a_request_still_to_be_translated_is_not_doubted_for_its_language(conn):
    ctx = make_ctx(conn)
    request = _request(ctx, language="zh")
    _body(conn, request.message_id, CHINESE)
    option = airline("1483.43")
    _session(ctx, request, [option])
    agent._quote(ctx, request, [option], None, fallback=False)
    assert "language_sent" not in {f["rule"] for f in _payloads(conn, "quote.checked")[-1]["findings"]}


async def test_a_distrusted_trucker_is_left_out_and_the_next_one_quoted(conn):
    """Round 67: a trucker's waiting charge per hour taken for the pickup price, rejected as too low."""
    from test_check import truck

    from pixel.pricing import calc

    ctx = make_ctx(conn)
    request = _request(ctx, door=True)
    air, cheap, fair = airline("1483.43"), truck("20.00", "Waitrate"), truck("150.00", "Elbkante")
    _session(ctx, request, [air, cheap, fair])
    wrong = calc.with_pickup(air, Decimal("20.00"), "Waitrate")
    sent = await agent._settled(ctx, request, [wrong], None, fallback=False, translate_by=None, repair_by=None)
    assert sent.option.trucker == "Elbkante" and sent.total == Decimal("1633.43")
    assert _payloads(conn, "option.vetoed") == [{"kind": "trucker", "name": "Waitrate", "rule": "implausible_bid"}]
    fallback = await agent.fallback(ctx, request.session_id)
    assert fallback is not None and fallback.option.trucker == "Elbkante"


def test_a_customer_who_asks_for_english_is_not_doubted_for_an_english_quote():
    option = airline("1483.43")
    german = (
        "Hallo zusammen, wir bitten um ein Angebot für die Luftfracht von FRA nach ORD, 679 kg, 4,19 cbm. Die Ware "
        "ist bereit und wir brauchen die günstigste Option. Bitte antworten Sie auf Englisch. Vielen Dank und Grüße"
    )
    assert "language_sent" not in rules(check.run(quote(rfq(), option), german, [option]))


def test_no_new_rule_blocks_a_correct_quote_on_any_request_fixture():
    """Every captured request, answered in the language we read for it with a quote that adds up: the rules this
    branch added find nothing to block (languages we translate are left out: the English draft would block)."""
    from pathlib import Path

    from pixel.api import mailbox

    new = {"language_sent", "insurance_missing", "excluded_carrier", "offer_pdf"}
    mails = Path(__file__).parent / "fixtures" / "mails"
    option, checked = airline("1483.43"), 0
    for path in sorted(mails.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
            message = mailbox.to_message(raw, "in")
        except Exception:  # noqa: BLE001, S112  not a single mail
            continue
        text = extract._text(message)
        language = extract._language(extract.own_words(text))
        if language not in compose.TEXT:
            continue
        request = rfq(language=language)
        _subject, body = compose.render(request, option, None, message.subject, "Lena Berger", text)
        premium = Decimal(1) if extract.insurance(extract.own_words(text)) else Decimal(0)
        found = check.run(quote(request, option, body), text, [option], insurance_eur=premium)
        assert not rules(found, check.BLOCK) & new, (path.name, found)
        checked += 1
    assert checked > 50


async def test_a_repaired_quote_keeps_the_further_shipments_of_the_mail(conn):
    ctx = make_ctx(conn)
    request = _request(ctx)
    cheap, good = bid("120.00", "Tiny Air"), airline("1483.43")
    _session(ctx, request, [cheap, good])

    async def extend(q):
        return q.model_copy(update={"body": q.body + "\n\nSecond shipment: as priced."})

    sent = await agent._settled(ctx, request, [cheap, good], None, fallback=False, translate_by=None, repair_by=None,
                                extend=extend)  # fmt: skip
    assert sent.total == Decimal("1483.43") and "Second shipment" in sent.body


# --- step 3: a weight or volume the mail does not explain, read again and priced again ----------------------------


def _at(option: RateOption, kg: str, total: str) -> RateOption:
    return option.model_copy(update={"chargeable_kg": Decimal(kg), "billed_kg": Decimal(kg), "freight": Decimal(total),
                                     "total": Decimal(total), "total_eur": Decimal(total)})  # fmt: skip


async def _settle(ctx, request, options, repriced):
    """`repair.settle` with the agent's build and check and a reprice that returns `repriced`."""
    seen = []

    async def reprice(changed):
        seen.append(changed)
        return repriced

    drafted = agent._quote(ctx, request, options, None, fallback=False)
    settled = await repair.settle(ctx, drafted, repair.Draft(request, options, None), lambda d: _built(ctx, d),
                                  lambda q: agent._findings(ctx, q.rfq, q), by=None, reprice=reprice)  # fmt: skip
    return settled, seen


def test_volume_accounting_blocks_cubic_feet_priced_as_cbm():
    """Round 79: "95.00 cubic feet" (2.69 cbm) priced as 95 cbm."""
    text = "Please quote FRA to ORD, 300 kg, a total volume of 95.00 cubic feet. Thanks"
    wrong = rfq(weight="300", volume="95")
    option = _at(airline("9000.00"), "15865", "9000.00")
    assert "volume_unexplained" in rules(check.run(quote(wrong, option), text, [option]), check.BLOCK)
    right = rfq(weight="300", volume="2.69")
    ok = _at(airline("900.00"), "449.23", "900.00")
    assert "volume_unexplained" not in rules(check.run(quote(right, ok), text, [ok]))


def test_a_volume_that_does_not_decide_the_chargeable_weight_is_not_doubted():
    text = "Please quote FRA to ORD, 3000 kg, 2 cbm. Thanks"
    option = _at(airline("9000.00"), "3000", "9000.00")
    assert "volume_unexplained" not in rules(check.run(quote(rfq(weight="3000", volume="3"), option), text, [option]))


async def test_cubic_feet_priced_as_cbm_are_priced_again_at_the_one_reading(conn):
    ctx = make_ctx(conn)
    text = "Please quote FRA to ORD, 300 kg, a total volume of 95.00 cubic feet. Thanks"
    request = _request(ctx, weight="300", volume="95")
    _body(conn, request.message_id, text)
    wrong = _at(airline("9000.00"), "15865", "9000.00")
    _session(ctx, request, [wrong])
    right = _at(airline("900.00"), "449.25", "900.00")
    settled, seen = await _settle(ctx, request, [wrong], [right])
    assert seen[0].volume_cbm.quantize(Decimal("0.01")) == Decimal("2.69") and "llm.call" not in kinds(ctx)
    assert settled.total == Decimal("900.00")
    assert session.rfq(ctx, request.session_id).volume_cbm == seen[0].volume_cbm  # the fallback reads it too
    assert {"kind": "chargeable", "kg": "15865", "rule": "volume_unexplained"} in _payloads(conn, "option.vetoed")
    assert wrong not in session.options(ctx, request.session_id)


async def test_lots_added_up_wrong_are_priced_again_at_their_sum(conn):
    """Round 9: three lots, priced on 1750 kg instead of their 1952 kg."""
    ctx = make_ctx(conn)
    text = (
        "Please quote FRA to ORD:\nLot 1: 4 cartons, each 48 kg\nLot 2: 2 pallets, 800 kg\nLot 3: 1 crate, 960 kg\n"
        "Thanks"
    )
    request = _request(ctx, weight="1750", volume="0")
    _body(conn, request.message_id, text)
    wrong, right = _at(airline("4000.00"), "1750", "4000.00"), _at(airline("4400.00"), "1952", "4400.00")
    _session(ctx, request, [wrong])
    settled, seen = await _settle(ctx, request, [wrong], [right])
    assert seen[0].weight_kg == Decimal(1952) and settled.total == Decimal("4400.00")


AMBIGUOUS = "Please quote FRA to ORD: 2 pallets of 300 kg each, total 600 kg. Thanks"


def _critic(monkeypatch, data=None, error=None):
    from pixel import llm
    from pixel.contracts import LlmResult

    calls = []

    async def ask_json(ctx, **kw):
        calls.append(kw)
        if error:
            raise error
        return LlmResult(data=data, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", ask_json)
    return calls


async def _ambiguous(conn, monkeypatch, data=None, error=None):
    ctx = make_ctx(conn)
    request = _request(ctx, weight="300", volume="0")
    _body(conn, request.message_id, AMBIGUOUS)
    wrong, right = _at(airline("900.00"), "300", "900.00"), _at(airline("1500.00"), "600", "1500.00")
    _session(ctx, request, [wrong])
    calls = _critic(monkeypatch, data, error)
    settled, seen = await _settle(ctx, request, [wrong], [right])
    return settled, seen, calls


async def test_the_critic_picks_among_code_readings_and_its_grounded_pick_is_priced(conn, monkeypatch):
    settled, seen, calls = await _ambiguous(conn, monkeypatch, {"value": 600, "quote": "total 600 kg"})
    assert len(calls) == 1 and calls[0]["purpose"] == "rfq.critic" and "Readings: 600 kg, 1200 kg" in calls[0]["user"]
    assert seen[0].weight_kg == Decimal(600) and settled.total == Decimal("1500.00")
    assert _payloads(conn, "rfq.critic")[0]["taken"] is True


@pytest.mark.parametrize(
    "data",
    [
        {"value": 600, "quote": "the total is 600 kg"},  # words the mail does not hold
        {"value": 450, "quote": "total 600 kg"},  # a figure of its own
        {"value": 1200, "quote": "total 600 kg"},  # a reading its words do not give
        {"value": None, "quote": ""},
    ],
)
async def test_an_ungrounded_critic_changes_nothing(conn, monkeypatch, data):
    settled, seen, _ = await _ambiguous(conn, monkeypatch, data)
    assert seen == [] and settled.total == Decimal("900.00")


async def test_a_failing_critic_changes_nothing(conn, monkeypatch):
    settled, seen, _ = await _ambiguous(conn, monkeypatch, error=TimeoutError())
    assert seen == [] and settled.total == Decimal("900.00") and "error" in kinds(make_ctx(conn))


async def test_no_reading_repair_without_a_price_for_it(conn):
    ctx = make_ctx(conn)
    text = "Please quote FRA to ORD, 300 kg, a total volume of 95.00 cubic feet. Thanks"
    request = _request(ctx, weight="300", volume="95")
    _body(conn, request.message_id, text)
    wrong = _at(airline("9000.00"), "15865", "9000.00")
    _session(ctx, request, [wrong])
    settled, seen = await _settle(ctx, request, [wrong], [])
    assert len(seen) == 1 and settled.total == Decimal("9000.00") and _payloads(conn, "option.vetoed") == []


# --- step 5: the prose call as a second reader -----------------------------------------------------------------------

TWO_GROUPS = "Please quote FRA to ORD. Group A: 4 pallets, 900 kg gross. Group B: 2 crates, 620 kg gross. 6 cbm. Thanks"


def _read(weight="1520", volume="6", destination="ORD"):
    return {"origin": "FRA", "destination": destination, "weight_kg": weight, "volume_cbm": volume, "pieces": 6,
            "lots": [], "preference": "cheapest", "door_pickup": False, "pickup_postcode": None, "currency": None,
            "language": "en", "first_name": None}  # fmt: skip


async def _second(conn, monkeypatch, figures, crosscheck=None, **settings):
    from pixel import llm
    from pixel.contracts import LlmResult
    from pixel.core.orchestrator import Deadline

    ctx = make_ctx(conn, **({"model_check": "gpt-4.1", "second_read": "on"} | settings))
    request = _request(ctx, weight="900", volume="6")  # the rules took group A alone
    _body(conn, request.message_id, TWO_GROUPS)
    events.emit(ctx, "rfq.second_read", "rfq", session_id=request.session_id, payload={"figures": figures})
    calls, priced = [], []

    async def ask_json(ctx, **kw):
        calls.append(kw["purpose"])
        return LlmResult(data=crosscheck or _read(), model="gpt-4.1", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    async def repriced(ctx, rfq, deadline, by):
        priced.append(rfq)
        return [_at(airline("3000.00"), str(calc.chargeable_kg(rfq.weight_kg, rfq.volume_cbm)), "3000.00")]

    from pixel.pricing import calc

    monkeypatch.setattr(llm, "ask_json", ask_json)
    monkeypatch.setattr(agent, "_repriced", repriced)
    message = __import__("pixel.contracts", fromlist=["Message"]).Message.model_validate_json(
        conn.execute("SELECT data FROM messages WHERE id = ?", (request.message_id,)).fetchone()[0]
    )
    old = [_at(airline("1900.00"), "1002", "1900.00")]
    deadline = Deadline(NOW, NOW, NOW, NOW)
    read, options = await agent._read_again(ctx, request, old, message, TWO_GROUPS, deadline)
    return ctx, request, read, options, calls, priced


async def test_a_second_reader_that_agrees_costs_no_further_call(conn, monkeypatch):
    _, request, read, options, calls, _ = await _second(conn, monkeypatch, {"destination": "ord", "weight_kg": 900.0,
                                                                            "volume_cbm": 6})  # fmt: skip
    assert calls == [] and read == request and options[0].total == Decimal("1900.00")


async def test_a_dissenting_second_reader_has_the_larger_model_read_and_a_grounded_figure_is_priced(conn, monkeypatch):
    ctx, request, read, options, calls, _ = await _second(
        conn, monkeypatch, {"destination": "ORD", "weight_kg": 1520, "volume_cbm": 6}
    )
    assert calls == ["rfq.crosscheck"] and read.weight_kg == Decimal(1520) and options[0].total == Decimal("3000.00")
    assert session.rfq(ctx, request.session_id).weight_kg == Decimal(1520)
    assert _payloads(conn, "option.vetoed") == [{"kind": "chargeable", "kg": "1002", "rule": "second_read"}]


async def test_a_larger_model_figure_code_cannot_ground_is_not_taken(conn, monkeypatch):
    _, request, read, _, calls, priced = await _second(
        conn, monkeypatch, {"destination": "ORD", "weight_kg": 1520, "volume_cbm": 6}, crosscheck=_read(weight="1600")
    )
    assert calls == ["rfq.crosscheck"] and read.weight_kg == request.weight_kg and priced == []


async def test_without_the_check_model_a_dissent_is_only_logged(conn, monkeypatch):
    _, request, read, _, calls, _ = await _second(
        conn, monkeypatch, {"destination": "ORD", "weight_kg": 1520, "volume_cbm": 6}, model_check=""
    )
    assert calls == [] and read == request


# --- step 4: a newer customer mail in the thread -----------------------------------------------------------------


async def test_a_quote_waits_briefly_for_a_newer_customer_mail_to_be_read(conn):
    from datetime import timedelta

    from pixel.core import timing

    ctx = make_ctx(conn)
    request = _request(ctx)
    later = json.loads(conn.execute("SELECT data FROM messages WHERE id = ?", (request.message_id,)).fetchone()[0])
    conn.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (?, ?, 'in', ?, 'pixel@hackloop.mail', 'Re', 'Sorry, 900 kg not 679', ?, ?)",
        (request.message_id + 1, request.session_id, request.customer, NOW.isoformat(), json.dumps(later)),
    )
    conn.execute("INSERT INTO mail_work(message_id) VALUES (?)", (request.message_id + 1,))
    assert agent._unread_followups(ctx, request) == [request.message_id + 1]
    await agent._followup_wait(ctx, request, timing.now() + timedelta(seconds=1))
    [waited] = _payloads(conn, "quote.followup_wait")
    assert waited["still_unread"] == [request.message_id + 1]
    conn.execute("UPDATE mail_work SET completed = 1")
    assert agent._unread_followups(ctx, request) == []


# --- figures from a later mail are never read back to the first one ---------------------------------------------


async def _later_case(conn, monkeypatch, *, later=False, answered=False):
    ctx = make_ctx(conn)
    request = _request(ctx, weight="1317", volume="0")  # the change mail's (round 86) or the answer's (round 104)
    _body(conn, request.message_id, "Please quote FRA to ORD, 1200 kg. Thanks")
    if answered:
        for id_, direction, purpose, reply in ((900001, "out", "question", None), (900002, "in", None, 900001)):
            conn.execute(
                "INSERT INTO messages (id, session_id, direction, purpose, sender, recipient, subject, body, in_reply_to,"
                " sent_at, data) VALUES (?, ?, ?, ?, ?, 'x', 's', '1317 kg', ?, ?, '{}')",
                (id_, request.session_id, direction, purpose, request.customer, reply, NOW.isoformat()),
            )
    priced = []

    async def repriced(ctx, rfq, deadline, by):
        priced.append(rfq)
        return [_at(airline("2600.00"), "1200", "2600.00")]

    monkeypatch.setattr(agent, "_repriced", repriced)
    option = _at(airline("2800.00"), "1317", "2800.00")
    _session(ctx, request, [option])
    sent = await agent._settled(ctx, request, [option], None, fallback=False, translate_by=None, repair_by=None,
                                later=later)  # fmt: skip
    return ctx, sent, priced


async def test_a_requote_keeps_the_changed_cargo(conn, monkeypatch):
    _, sent, priced = await _later_case(conn, monkeypatch, later=True)
    assert priced == [] and sent.total == Decimal("2800.00") and sent.rfq.weight_kg == Decimal(1317)
    assert _payloads(conn, "quote.checked")[-1]["blocking"]  # still checked: a person sees the doubt


async def test_a_weight_from_the_answer_to_our_question_is_kept(conn, monkeypatch):
    ctx, sent, priced = await _later_case(conn, monkeypatch, answered=True)
    assert agent._later_figures(ctx, sent.rfq)
    assert priced == [] and sent.total == Decimal("2800.00")


async def test_without_a_later_mail_the_same_doubt_is_read_again(conn, monkeypatch):
    _, sent, priced = await _later_case(conn, monkeypatch)
    assert priced[0].weight_kg == Decimal(1200) and sent.total == Decimal("2600.00")


async def test_a_rejected_reading_leaves_its_prices_out_of_the_fallback(conn):
    ctx = make_ctx(conn)
    text = "Please quote FRA to ORD, 300 kg, a total volume of 95.00 cubic feet. Thanks"
    request = _request(ctx, weight="300", volume="95")
    _body(conn, request.message_id, text)
    wrong = _at(airline("9000.00"), "15865", "9000.00")
    _session(ctx, request, [wrong])
    from pixel.pricing import calc

    kg = calc.chargeable_kg(Decimal(300), Decimal("95.00") * check.FT3)
    short = _at(airline("700.00"), str(kg), "700.00").model_copy(update={"billed_kg": Decimal(300)})  # still blocks
    events.emit(ctx, "option.found", "pricing", session_id=request.session_id,
                payload={"option": short.model_dump(mode="json")})  # fmt: skip
    settled, _ = await _settle(ctx, request, [wrong], [short])
    assert settled.total == Decimal("9000.00")  # the guard turned the reading down
    assert [p["rule"] for p in _payloads(conn, "option.vetoed")] == ["volume_unexplained:rejected"]
    assert short not in session.options(ctx, request.session_id) and wrong in session.options(ctx, request.session_id)


@pytest.mark.parametrize("mode", ["log", "off"])
async def test_the_second_reader_can_only_log_or_be_off(conn, monkeypatch, mode):
    _, request, read, _, calls, _ = await _second(
        conn, monkeypatch, {"destination": "ORD", "weight_kg": 1520, "volume_cbm": 6}, second_read=mode
    )
    assert calls == [] and read == request
