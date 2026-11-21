"""The full-body draft (`compose.draft`, PIXEL_DRAFT): the model writes the mail with placeholders, code fills every
value, and any doubt gives the template instead."""

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from test_bids import IDS, FakePortal, deadline, kinds, mail

from pixel import config, llm
from pixel.agents.rfq import agent, compose, dangerous
from pixel.agents.rfq.extract import DG_DOCUMENTS
from pixel.config import settings
from pixel.contracts import LaneTariff, LlmResult, Message, RateOption, Rfq, Source
from pixel.core import events, outbox
from pixel.core.context import Context
from pixel.core.orchestrator import Deadline
from pixel.crm import quotes
from pixel.pricing import portal_rates, store

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


@pytest.fixture
def ctx(conn, monkeypatch):
    monkeypatch.setattr(portal_rates, "_lanes", None)
    store.save_carriers(ctx := Context(settings(), conn, None, FakePortal(), None, None), [
        {"name": "Air One", "kind": "airline", "email": "one@air", "serves": "BBB"},
        {"name": "Air Two", "kind": "airline", "email": "two@air", "serves": "BBB"},
        {"name": "Truck A", "kind": "trucker", "email": "a@truck", "serves": "12"},
    ])  # fmt: skip
    store.save_zones(ctx, [{"zone": "12", "area": "Town", "airport": "AAA"}])
    return ctx


OPTION = RateOption(
    source=Source.SHEET, carrier="Air One", origin="AAA", destination="BBB", chargeable_kg=D(100),
    billed_kg=D(100), freight=D("200.00"), fuel=D("50.00"), security=D("10.00"), total=D("260.00"),
    currency="EUR", total_eur=D("260.00"), transit_days=3,
)  # fmt: skip

CASUAL = (
    "Hi Lena,\n\nHope your week's going well! Could you quote AAA to BBB (Example City, XX), 100 kg of spare "
    "parts? Our buyer is waiting on them.\n\nCheers,\nTom"
)
FORMAL = (
    "Dear Ms Berger,\n\nWe would like to request a quotation for 100 kg of laboratory glassware from AAA to BBB. "
    "The goods are ready on 2026-11-25.\n\nYours sincerely\nJonas Kessler\nPurchasing"
)
GERMAN = (
    "Sehr geehrte Frau Berger,\n\nbitte senden Sie uns ein Angebot für 100 kg Ersatzteile von AAA nach BBB.\n\n"
    "Mit freundlichen Grüßen\nAnna Muster"
)

CASUAL_BODY = """<SAL>

Going well here, thanks for asking, and good to hear from you. Glad to help get the spare parts to your buyer quickly.

{{CARRIER}} can fly them from {{ORIGIN}} to {{DEST_CITY}} for {{TOTAL}}, with {{TRANSIT}} in transit. That covers the \
air freight plus the fuel and security surcharges, so there is nothing else to add on top.

If that works for you, just reply and I'll book it right away. Happy to look again if the timing changes on your side.

{{SIGNATURE}}"""

FORMAL_BODY = """<SAL>

Thank you for your message. The laboratory glassware can travel with {{CARRIER}} from {{ORIGIN}} to {{DEST_CITY}}, \
and the all-in price is {{TOTAL}} with {{TRANSIT}} in transit.

With the cargo ready on {{READY}}, the earliest arrival would be {{ARRIVAL}}. The price includes the air freight \
together with the fuel and security surcharges.

Should you wish to proceed, a short reply is sufficient and I will arrange the booking with the airline.

{{SIGNATURE}}"""

GERMAN_BODY = """<SAL>

vielen Dank für Ihre Nachricht. Für die Ersatzteile kann ich Ihnen einen Flug mit {{CARRIER}} von {{ORIGIN}} nach \
{{DEST_CITY}} anbieten, die Laufzeit beträgt {{TRANSIT}}.

Der Preis liegt bei {{TOTAL}} all-in und umfasst die Luftfracht sowie den Treibstoff- und den Sicherheitszuschlag. \
Weitere Kosten kommen nicht hinzu.

Wenn das für Sie passt, antworten Sie mir bitte kurz, dann kümmere ich mich gerne um die Buchung bei der Airline.

{{SIGNATURE}}"""


def rfq(language: str = "en", session_id: int = 1, **kw) -> Rfq:
    return Rfq(session_id=session_id, message_id=1, customer="c@x", origin="AAA", destination="BBB",
               weight_kg=D(100), language=language, received_at=NOW, deadline_at=NOW, **kw)  # fmt: skip


def message(body: str) -> Message:
    return Message(id=1, session_id=1, direction="in", sender="c@x", to="us@x", subject="Quote please", body=body,
                   sent_at=NOW)  # fmt: skip


def answering(ctx, monkeypatch, body: str | None, first_name: str | None = None, calls: list | None = None):
    """The model answers `body`, with the salutation code offered it put in for <SAL>."""

    async def model(ctx, **kw):
        if calls is not None:
            calls.append(kw)
        events.emit(ctx, "llm.call", "llm", session_id=kw["session_id"], payload={}, cost_usd=D("0.0001"))
        salutation = re.search(r"^salutation: (.*)$", kw["user"], re.MULTILINE)
        text = body.replace("<SAL>", salutation.group(1) if salutation else "") if body is not None else None
        return LlmResult(data={"first_name": first_name, "body": text}, model="m", tokens_in=1, tokens_out=1,
                         cost_usd=D(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", model)


def last_prose(ctx) -> dict:
    return kinds(ctx, "quote.prose")[-1]


async def test_a_casual_english_draft_is_filled_by_code(ctx, monkeypatch):
    calls = []
    answering(ctx, monkeypatch, CASUAL_BODY, "Tom", calls)
    body = await compose.draft(ctx, rfq(), OPTION, message(CASUAL))
    assert body and body.startswith(("Hi Tom,\n\n", "Hello Tom,\n\n")) and "{{" not in body
    assert body.count("260.00 EUR") == 1 and "Air One can fly them from AAA to Example City" in body
    assert body.endswith(f"Cheers,\nLena\n{compose.COMPANY}") and compose.check(body, rfq(), OPTION)
    assert calls[0]["purpose"] == "quote.draft" and calls[0]["max_output_tokens"] == 320
    assert calls[0]["schema"] == compose.DRAFT_SCHEMA and "260" not in calls[0]["user"].split("Customer's mail")[0]
    assert "register: casual" in calls[0]["user"] and "claim: none" in calls[0]["user"]
    event = last_prose(ctx)
    assert event["mode"] == "draft" and event["passed"] and event["first_name"] == "Tom"
    assert event["intro"].startswith("Going well here")


async def test_a_formal_english_draft_keeps_the_register(ctx, monkeypatch):
    answering(ctx, monkeypatch, FORMAL_BODY)
    body = await compose.draft(ctx, rfq(), OPTION, message(FORMAL))
    assert body.startswith("Dear Jonas Kessler,\n\nThank you") and "!" not in body
    assert "ready on Wednesday 25 November, the earliest arrival would be Saturday 28 November" in body
    assert body.endswith(f"Kind regards,\nLena Berger\n{compose.COMPANY}")


async def test_a_formal_german_draft_uses_sie_and_the_whole_name(ctx, monkeypatch):
    calls = []
    answering(ctx, monkeypatch, GERMAN_BODY, calls=calls)
    body = await compose.draft(ctx, rfq("de"), OPTION, message(GERMAN))
    assert body.startswith("Guten Tag Anna Muster,\n\nvielen Dank") and "260.00 EUR all-in" in body
    assert "Laufzeit beträgt 3 Tage" in body and body.endswith(
        f"Mit freundlichen Grüßen\nLena Berger\n{compose.COMPANY}"
    )
    assert "salutation: Guten Tag {{NAME}}," in calls[0]["user"] and "register: formal" in calls[0]["user"]


async def test_a_bulky_draft_must_name_the_chargeable_weight(ctx, monkeypatch):
    bulky = OPTION.model_copy(update={"chargeable_kg": D(200), "billed_kg": D(200)})
    with_weight = CASUAL_BODY.replace(
        "so there is nothing else to add on top.",
        "and because of the volume it is charged on {{CHARGEABLE}} rather than the gross {{GROSS}}.",
    )
    answering(ctx, monkeypatch, with_weight)
    assert "charged on 200 kg rather than the gross 100 kg" in await compose.draft(ctx, rfq(), bulky, message(CASUAL))
    answering(ctx, monkeypatch, CASUAL_BODY)
    assert await compose.draft(ctx, rfq(), bulky, message(CASUAL)) is None
    assert "CHARGEABLE" in last_prose(ctx)["reason"]


async def test_a_dangerous_goods_draft_names_the_goods_and_asks_for_the_papers(ctx, monkeypatch):
    dg = CASUAL_BODY.replace(
        "Happy to look again if the timing changes on your side.",
        "As you wrote, the {{DG_GOODS}} travel as dangerous goods, so please send the DGD and the MSDS first.",
    )
    answering(ctx, monkeypatch, dg)
    body = await compose.draft(ctx, rfq(), OPTION, message(CASUAL), dg="UN1950, Aerosols")
    assert "the UN1950, Aerosols travel as dangerous goods" in body
    answering(ctx, monkeypatch, dg.replace(" and the MSDS", ""))
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL), dg="UN1950, Aerosols") is None
    answering(ctx, monkeypatch, CASUAL_BODY)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL), dg="UN1950, Aerosols") is None


def broken(old: str, new: str):
    return lambda body: body.replace(old, new, 1)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (broken("{{TRANSIT}} in transit", "3 days in transit"), "TRANSIT"),
        (broken("good to hear", "good to hear for the 2nd time"), "digit"),
        (broken("{{TOTAL}}", "{{TOTAL}} ({{PRICE}})"), "not offered"),
        (broken("{{TOTAL}}", "{{TOTAL}}, that is {{TOTAL}}"), "TOTAL 2"),
        (broken("{{TOTAL}}", "a fair price"), "TOTAL 0"),
        (broken("{{SIGNATURE}}", "Best, Lena"), "SIGNATURE 0"),
        (lambda b: b.replace("\n\n{{SIGNATURE}}", "") + "\n\n{{SIGNATURE}}\n\nTalk soon.", "signature not last"),
        (broken("{{CARRIER}} can fly them", "We can fly them"), "CARRIER"),
        (broken("{{DEST_CITY}}", "the destination"), "DEST_CITY"),
        (broken("Glad to help", "{TOTAL} Glad to help"), "malformed"),
        (broken("quickly.", "quickly. 🙂"), "emoji"),
        (lambda b: b.replace("\n\n{{SIGNATURE}}", "\n\nP.S. Ask me anything.\n\n{{SIGNATURE}}"), "P.S."),
        (broken("on top.", "on top — promise."), "dash"),
        (broken("on top.", "on top – promise."), "dash"),
        (broken("Glad to help", "Rest assured, glad to help"), "stock phrase"),
        (broken("Glad to help", "Great news, glad to help"), "stock phrase"),
        (broken("Happy to look again", "Please don't hesitate to ask, happy to look again"), "stock phrase"),
        (broken("quickly.", "quickly! Really!"), "exclamation"),
        (broken("right away.", "right away, it is already booked."), "booking status"),
        (broken("right away.", "right away and it is reserved for you."), "booking status"),
        (broken("can fly them", "is the cheapest to fly them"), "cheapest"),
        (broken("can fly them", "is the fastest to fly them"), "fastest"),
        (broken("can fly them", "is the best way to fly them"), "best"),
        (broken("Glad to help", "Air Two was full, glad to help"), "carrier"),
        (broken("Glad to help", "Lena is glad to help"), "signer"),
        (broken("Glad to help", "Tom, glad to help"), "name"),
        (broken("{{DEST_CITY}}", "{{DEST_CITY}} near Example City"), "name or place"),
        (broken("Glad to help", "The FRA team is glad to help"), "code"),
        (broken("Glad to help", "Glad to help on Monday"), "weekday"),
        (broken("Glad to help", "See www.example.com, glad to help"), "symbol"),
        (broken("Going well here", "- Going well here"), "bullet"),
        (lambda b: b.split("\n\n", 1)[1], "salutation"),
        (lambda b: "\n\n".join(b.split("\n\n")[:1] + b.split("\n\n")[2:3] + ["{{SIGNATURE}}"]), "words"),
    ],
)
async def test_every_failed_check_drops_the_draft(ctx, monkeypatch, change, reason):
    answering(ctx, monkeypatch, None)
    facts = compose.facts(ctx, rfq(), OPTION, CASUAL)
    body = change(CASUAL_BODY.replace("<SAL>", facts.salutation))
    answering(ctx, monkeypatch, body)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL)) is None
    event = last_prose(ctx)
    assert event["mode"] == "draft" and not event["passed"] and reason in event["reason"]
    assert event["rejected"] == body.strip()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (broken("Thank you for your message.", "Thank you for your message!"), "exclamation"),
        (broken("{{ARRIVAL}}", "soon"), "ARRIVAL"),
        (broken("Thank you for your message.", "Thanks for reaching out."), "stock phrase"),
    ],
)
async def test_a_formal_draft_with_an_exclamation_or_a_missing_date_is_dropped(ctx, monkeypatch, change, reason):
    answering(ctx, monkeypatch, change(FORMAL_BODY))
    assert await compose.draft(ctx, rfq(), OPTION, message(FORMAL)) is None
    assert reason in last_prose(ctx)["reason"]


async def test_german_du_and_an_english_body_to_a_german_mail_are_dropped(ctx, monkeypatch):
    answering(ctx, monkeypatch, GERMAN_BODY.replace("antworten Sie mir bitte kurz", "antworte mir kurz, wenn du magst"))
    assert await compose.draft(ctx, rfq("de"), OPTION, message(GERMAN)) is None
    assert last_prose(ctx)["reason"] == "du"
    english = CASUAL_BODY.replace(" to {{DEST_CITY}}", " to {{DEST_CITY}} from {{ORIGIN}}")
    answering(ctx, monkeypatch, english)
    assert await compose.draft(ctx, rfq("de"), OPTION, message(GERMAN)) is None
    assert last_prose(ctx)["reason"] in ("language", "salutation")


@pytest.mark.parametrize("phrase", ["before I can book it", "I'll book it", "I will arrange the booking"])
async def test_offering_to_book_is_no_status_claim(ctx, monkeypatch, phrase):
    answering(ctx, monkeypatch, CASUAL_BODY.replace("I'll book it right away", phrase))
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL))


async def test_a_superlative_passes_only_when_code_licensed_it(ctx, monkeypatch):
    cheapest = CASUAL_BODY.replace("can fly them", "is the cheapest way to fly them")
    answering(ctx, monkeypatch, cheapest)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL), claimed_wish="cheapest")
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL), claimed_wish="fastest") is None
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL), claimed_wish="anything") is None


async def test_an_opening_this_customer_already_got_is_not_sent_again(ctx, monkeypatch):
    for session in (1, 2):
        ctx.db.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (?, 'c@x', 'new', 't')",
                       (session,))  # fmt: skip
    calls = []
    answering(ctx, monkeypatch, CASUAL_BODY, calls=calls)
    assert await compose.draft(ctx, rfq(session_id=1), OPTION, message(CASUAL))
    assert await compose.draft(ctx, rfq(session_id=2), OPTION, message(CASUAL)) is None
    assert last_prose(ctx)["reason"] == "opening repeats an earlier one"
    assert "returning customer: yes" in calls[1]["user"] and "Going well here" in calls[1]["user"]


async def test_a_missing_pickup_postcode_must_be_asked_for(ctx, monkeypatch):
    door = rfq(door_pickup=True)
    option = OPTION.model_copy(update={"trucker": "Truck A", "pickup": D(40), "total": D("300.00"),
                                       "total_eur": D("300.00")})  # fmt: skip
    body = CASUAL_BODY.replace("can fly them", "collects them with {{TRUCKER}} and flies them")
    answering(ctx, monkeypatch, body)
    assert await compose.draft(ctx, door, option, message(CASUAL)) is None
    assert last_prose(ctx)["reason"] == "pickup postcode not asked"
    asked = body.replace("Happy to look", "Please send the full pickup address with the postcode. Happy to look")
    answering(ctx, monkeypatch, asked)
    assert "collects them with Truck A" in await compose.draft(ctx, door, option, message(CASUAL))
    answering(ctx, monkeypatch, asked.replace("{{TRUCKER}}", "Truck A"))
    assert await compose.draft(ctx, door, option, message(CASUAL)) is None


async def test_an_air_only_price_never_gets_a_draft(ctx, monkeypatch):
    calls = []
    answering(ctx, monkeypatch, CASUAL_BODY, calls=calls)
    assert await compose.draft(ctx, rfq(door_pickup=True), OPTION, message(CASUAL)) is None and not calls


@pytest.mark.parametrize("answer", [None, "", "   "])
async def test_an_empty_answer_is_dropped(ctx, monkeypatch, answer):
    answering(ctx, monkeypatch, answer)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL)) is None
    assert last_prose(ctx)["reason"] == "no body"


async def test_a_gateway_failure_gives_none_and_an_error(ctx, monkeypatch):
    async def fail(ctx, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", fail)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL)) is None
    assert last_prose(ctx)["reason"] == "gateway: RuntimeError"
    assert kinds(ctx, "error")[-1] == {"where": "draft", "error": "RuntimeError"}


async def test_a_broken_check_never_raises(ctx, monkeypatch):
    answering(ctx, monkeypatch, CASUAL_BODY)
    monkeypatch.setattr(compose, "vet", lambda *a: 1 / 0)
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL)) is None
    assert last_prose(ctx)["reason"] == "ZeroDivisionError"


async def test_a_passed_draft_gives_the_fallback_its_first_name_but_not_its_opening(ctx, monkeypatch):
    answering(ctx, monkeypatch, CASUAL_BODY, "Tom")
    assert await compose.draft(ctx, rfq(), OPTION, message(CASUAL))
    assert compose.stored(ctx, 1) == compose.Prose(first_name="Tom")


# --- wired into the agent ----------------------------------------------------------------------------------------


@pytest.fixture
def sheet(ctx, monkeypatch):
    store.upsert(ctx, [LaneTariff(
        carrier="Air One", origin="AAA", destination="BBB", currency="EUR", valid_from=date(2000, 1, 1),
        valid_to=date(2100, 1, 1), version="v", transit_days=3, minimum=D(50), breaks=((D(0), D("2.00")),),
        fuel_per_kg=D("0.50"), security_per_kg=D("0.10"), source="test",
    )])  # fmt: skip
    ctx.db.execute("DELETE FROM carriers")

    async def send_quote(ctx, quote):
        return quote.model_copy(update={"sent_at": datetime.now(UTC), "sent_message_id": 9})

    monkeypatch.setattr(outbox, "send_quote", send_quote)
    monkeypatch.setattr(quotes, "new_id", lambda ctx: f"Q-{next(IDS)}")
    return ctx


REQUEST = "Hi,\n\nHope your week's going well! Please quote AAA-BBB, 100 kg, 0.3 cbm.\n\nCheers,\nTom"


async def test_the_agent_sends_the_draft_with_one_call_and_the_fallback_makes_none(sheet, monkeypatch):
    calls = []
    answering(sheet, monkeypatch, CASUAL_BODY.replace("the spare parts to your buyer", "it there"), "Tom", calls)
    quote = await agent.handle(sheet, mail("c@x", REQUEST), deadline(30))
    assert [c["purpose"] for c in calls] == ["quote.draft"] and "Air One can fly them from AAA to BBB" in quote.body
    assert quote.body.count("260.00 EUR") == 1 and quote.llm_calls == 1
    backup = await agent.fallback(sheet, 1)
    assert len(calls) == 1 and backup.body.startswith(compose.greeting("en", "Tom", 1) + "\n\n")
    assert "Air One can fly them" not in backup.body and backup.total == quote.total


async def test_a_failed_draft_sends_the_template(sheet, monkeypatch):
    answering(sheet, monkeypatch, "Hi Tom, it is 260 EUR. {{SIGNATURE}}")
    quote = await agent.handle(sheet, mail("c@x", REQUEST), deadline(30))
    assert quote.body.startswith(compose.greeting("en", "Tom", 1) + "\n\n") and "{{" not in quote.body
    assert last_prose(sheet)["reason"] == "TOTAL 0 times"
    assert quote.body.count("260.00 EUR") == 1 and quote.llm_calls == 1


async def test_a_hanging_draft_never_holds_the_quote(sheet, monkeypatch):
    async def hangs(ctx, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(agent, "PROSE_WAIT_S", 0.3)
    monkeypatch.setattr(agent, "DRAFT_MIN_S", 0.1)
    monkeypatch.setattr(llm, "ask_json", hangs)
    started = datetime.now(UTC)
    quote = await agent.handle(sheet, mail("c@x", REQUEST), deadline(30))
    assert (datetime.now(UTC) - started).total_seconds() < 2 and "260.00 EUR" in quote.body
    assert last_prose(sheet)["reason"] == "timeout: TimeoutError"


async def test_the_draft_may_hold_the_quote_a_little_past_target_but_never_past_hard(sheet, monkeypatch):
    calls = []
    answering(sheet, monkeypatch, CASUAL_BODY.replace("the spare parts to your buyer", "it there"), calls=calls)
    now = datetime.now(UTC)
    late = Deadline(received_at=now, target_at=now - timedelta(seconds=1), hard_at=now + timedelta(seconds=60),
                    expires_at=now + timedelta(seconds=60))  # fmt: skip
    assert "Air One can fly them" in (await agent.handle(sheet, mail("c@x", REQUEST), late)).body
    later = replace(late, target_at=now - timedelta(seconds=agent.DRAFT_PAST_TARGET_S))
    assert "Air One can fly them" not in (await agent.handle(sheet, mail("c@x", REQUEST, 2), later)).body
    assert len(calls) == 1 and last_prose(sheet)["reason"] == "no time left"


async def test_without_time_left_the_template_goes_at_once(sheet, monkeypatch):
    calls = []
    answering(sheet, monkeypatch, CASUAL_BODY, calls=calls)
    quote = await agent.handle(sheet, mail("c@x", REQUEST), deadline(0.5))
    assert not calls and "260.00 EUR" in quote.body
    assert last_prose(sheet) == {"mode": "draft", "passed": False, "reason": "no time left"}


async def test_a_request_without_a_price_yet_gets_the_slots_for_the_fallback_and_no_draft(sheet, monkeypatch):
    calls = []

    async def model(ctx, **kw):
        calls.append(kw["purpose"])
        return LlmResult(data={"first_name": "Tom", "hook": None, "intro": None, "closing": None}, model="m",
                         tokens_in=1, tokens_out=1, cost_usd=D(0))  # fmt: skip

    async def nothing(ctx, rfq, deadline):
        return []

    monkeypatch.setattr(llm, "ask_json", model)
    monkeypatch.setattr(agent.engine, "options", nothing)
    assert await agent.handle(sheet, mail("c@x", REQUEST), deadline(30)) is None
    for _ in range(50):
        if compose.stored(sheet, 1):
            break
        await asyncio.sleep(0.01)
    assert calls == ["quote.prose"] and compose.stored(sheet, 1).first_name == "Tom"


async def test_draft_off_keeps_the_prose_slots(sheet, monkeypatch):
    calls = []

    async def model(ctx, **kw):
        calls.append(kw["purpose"])
        return LlmResult(data={"first_name": None, "hook": None, "intro": None, "closing": None}, model="m",
                         tokens_in=1, tokens_out=1, cost_usd=D(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", model)
    sheet.settings = replace(sheet.settings, draft_quotes=False)
    quote = await agent.handle(sheet, mail("c@x", REQUEST), deadline(30))
    assert calls == ["quote.prose"] and "260.00 EUR" in quote.body


async def test_a_draft_that_asks_for_the_papers_gets_no_second_dangerous_goods_note(sheet, monkeypatch):
    monkeypatch.setattr(dangerous, "declared", lambda ctx, rfq: ("UN1950, Aerosols", "mail"))
    request = rfq(missing=(DG_DOCUMENTS,))
    asked = "Please send the shipper's declaration for dangerous goods (DGD) and the safety data sheet (MSDS)."
    quote = agent._quote(sheet, request, [OPTION], None, fallback=False,
                         drafted=(OPTION, f"Hi,\n\nUN1950, Aerosols. {asked} 260.00 EUR"))  # fmt: skip
    assert quote.body.count("DGD") == 1
    template = agent._quote(sheet, request, [OPTION], None, fallback=True)
    assert "UN1950, Aerosols" in template.body and "(DGD)" in template.body
    other = agent._quote(sheet, request, [OPTION], None, fallback=False,
                         drafted=(OPTION.model_copy(update={"carrier": "Air Two"}), "written for another option"))  # fmt: skip
    assert "written for another option" not in other.body


def test_the_claim_needs_two_options_and_the_customers_wish():
    slow = OPTION.model_copy(update={"carrier": "Air Two", "total_eur": D(300), "total": D(300), "transit_days": 1})
    assert agent._claim(rfq(), OPTION, [OPTION]) is None
    assert agent._claim(rfq(), OPTION, [OPTION, slow]) == "cheapest"
    assert agent._claim(rfq(), slow, [OPTION, slow]) is None
    assert agent._claim(rfq(preference="fastest"), slow, [OPTION, slow]) == "fastest"
    assert agent._claim(rfq(preference="fastest"), OPTION, [OPTION, slow]) is None


@pytest.mark.parametrize(("value", "on"), [(None, True), ("on", True), ("0", False), ("off", False), ("FALSE", False)])
def test_pixel_draft_switch(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("PIXEL_DRAFT", raising=False)
    else:
        monkeypatch.setenv("PIXEL_DRAFT", value)
    config.settings.cache_clear()
    try:
        assert config.settings().draft_quotes is on
    finally:
        config.settings.cache_clear()


def test_the_prices_of_the_larger_models_are_known():
    assert llm._price("gpt-4.1") == (D("2.00"), D("8.00")) and llm._price("gpt-4o") == (D("2.50"), D("10.00"))


def test_the_draft_prompt_offers_only_the_placeholders_that_apply(ctx):
    facts = compose.facts(ctx, rfq(), OPTION, CASUAL)
    user = compose._draft_user(rfq(), message(CASUAL), CASUAL, facts)
    offered = set(re.findall(r"^\{\{([A-Z_]+)\}\}", user, re.MULTILINE))
    assert offered == {"NAME", "TOTAL", "CARRIER", "TRANSIT", "ORIGIN", "DEST_CITY", "GROSS", "SIGNATURE"}
    assert not re.search(r"\d", user.split("Customer's mail")[0].replace("ISO", ""))
    assert json.dumps(facts.values)  # every value is a plain string code formatted
    assert facts.values["DEST_CITY"] == "Example City" and facts.values["TOTAL"] == "260.00 EUR"
    later = compose.facts(ctx, rfq(), OPTION.model_copy(update={"valid_until": NOW + timedelta(minutes=15)}), FORMAL)
    assert {"READY", "ARRIVAL", "DEPARTURE", "VALID_UNTIL"} <= set(later.values) and "VALID_UNTIL" in later.required
