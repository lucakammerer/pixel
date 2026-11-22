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
    mail = ingest_rfq(ctx)
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

    async def worse(ctx, quote, draft, by, build):
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
