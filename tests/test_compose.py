import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_extract import DEADLINE, captured

from pixel import llm
from pixel.agents.rfq import compose, dangerous, extract
from pixel.config import settings
from pixel.contracts import LlmResult, Message, RateOption, Rfq, Source
from pixel.core.context import Context

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
SIGNER = "Lena Berger"


def rfq(language: str, session_id: int = 1) -> Rfq:
    return Rfq(
        session_id=session_id, message_id=1, customer="buyer@example.com", origin="AAA", destination="BBB",
        weight_kg=Decimal(1200), language=language, received_at=NOW, deadline_at=NOW,
    )  # fmt: skip


def message(
    body: str = "Please quote AAA-BBB, 1200 kg of machine parts, we are in a hurry.\n\nThanks, Anna",
) -> Message:
    return Message(id=1, session_id=1, direction="in", sender="b@example.com", to="u@example.com",
                   subject="Machine parts to BBB", body=body, sent_at=NOW)  # fmt: skip


OPTION = RateOption(
    source=Source.SHEET, carrier="Example Air", origin="AAA", destination="BBB", chargeable_kg=Decimal(1200),
    billed_kg=Decimal(1200), freight=Decimal("1000.00"), fuel=Decimal("200.00"), security=Decimal("34.565"),
    total=Decimal("1234.565"), currency="EUR", total_eur=Decimal("1234.565"), transit_days=3,
)  # fmt: skip


@pytest.fixture
def ctx(conn):
    conn.execute("INSERT INTO carriers VALUES ('Example Air', 'airline', 'x@air', 'BBB')")
    conn.execute(
        "INSERT INTO messages (id, session_id, direction, sender, recipient, subject, body, sent_at, data)"
        " VALUES (1, 1, 'in', 'b@example.com', 'u@example.com', 'Machine parts to BBB', '', ?, '{}')",
        (NOW.isoformat(),),
    )
    return Context(settings(), conn, None, None, None, None)


@pytest.mark.parametrize("session_id", [1, 2])
def test_total_is_written_once_without_a_thousands_separator_in_every_language(session_id):
    _, en = compose.render(rfq("en", session_id), OPTION)
    _, de = compose.render(rfq("de", session_id), OPTION)
    assert en.count("1234.57 EUR") == 1 and de.count("1234.57 EUR") == 1
    assert "1,234" not in en and "1.234" not in de
    assert "Example Air" in en and "3 days" in en and "3 Tage" in de
    assert compose.check(en, rfq("en"), OPTION) and compose.check(de, rfq("de"), OPTION)


def test_pickup_is_named_at_the_place_the_customer_names():
    option = OPTION.model_copy(update={"trucker": "Example Trucks", "pickup": Decimal(50)})
    _, body = compose.render(rfq("en"), option)
    assert "your site by Example Trucks" in body
    _, body = compose.render(rfq("en"), option, request="Hi,\n\nplease include pickup from our warehouse.\n\nSara")
    assert "your warehouse by Example Trucks" in body


def test_reply_subject_name_and_signer():
    prose = compose.Prose(first_name="Anna", intro="Happy to help get the machine parts moving quickly.",
                          closing="Have a good day.")  # fmt: skip
    subject, body = compose.render(rfq("en"), OPTION, prose, "Machine parts to BBB", SIGNER)
    assert subject == "Re: Machine parts to BBB"
    # The model's opening leads into the offer, below the greeting (R31).
    assert body.startswith("Hello Anna,\n\nHappy to help get the machine parts moving quickly. ")
    # The mail ends on the booking sentence and the signature: no stock closing line, no P.S. (R31).
    assert "Have a good day" not in body and body.endswith(f"Best regards,\n{SIGNER}\n{compose.COMPANY}")
    assert compose.reply_subject("AW: Anfrage") == "AW: Anfrage" and compose.reply_subject("") is None


def test_without_subject_or_prose_still_a_full_mail():
    subject, body = compose.render(rfq("de"), OPTION)
    assert subject == "Angebot AAA nach BBB, 1.200 kg"
    assert body.startswith("Guten Tag,\n\n") and "Viele Grüße" in body


@pytest.mark.parametrize(
    ("slot", "text", "language"),
    [
        ("intro", "Your 1200 kg are welcome.", "en"),
        ("intro", "We can route this via JFK for you.", "en"),
        ("intro", "Example Air will be glad to carry it.", "en"),
        ("intro", "We can pick it up on Monday.", "en"),
        ("intro", "That would cost you about € a lot.", "en"),
        ("intro", "Hello and thanks for your mail.", "en"),
        ("intro", "Danke für deine Anfrage, das machen wir gerne.", "de"),
        ("intro", "Thanks for the request, we will handle the shipment for you.", "de"),
        ("intro", "- Thanks for your request, we will help.", "en"),
        ("intro", "Thanks " * 50, "en"),
        ("closing", "Best regards from the team.", "en"),
    ],
)
def test_bad_slot_is_dropped(slot, text, language):
    prose = compose.clean({slot: text}, rfq(language), message(), SIGNER, {"example air"})
    assert getattr(prose, slot) is None


def test_good_slots_pass_and_the_name_must_be_in_the_mail():
    raw = {"first_name": "Anna", "hook": "we are in a hurry", "intro": "Understood, I kept speed first on this one.",
           "closing": "Have a good afternoon."}  # fmt: skip
    assert compose.clean(raw, rfq("en"), message(), SIGNER) == compose.Prose(
        first_name="Anna", intro=raw["intro"], closing=raw["closing"]
    )
    assert compose.clean({"first_name": "Maria"}, rfq("en"), message(), SIGNER).first_name is None
    assert compose.clean({"first_name": "Lena"}, rfq("en"), message("Thanks, Lena"), SIGNER).first_name is None


def test_render_drops_slots_that_break_the_total():
    prose = compose.Prose(intro="It is 1234.57 EUR anyway.")  # unchecked on purpose
    _, body = compose.render(rfq("en"), OPTION, prose)
    assert body.count("1234.57") == 1 and "anyway" not in body


async def test_prose_is_stored_for_the_fallback(ctx, monkeypatch):
    calls = []

    async def fake_ask(ctx, **kw):
        calls.append(kw)
        return LlmResult(data={"first_name": "Anna", "hook": "we are in a hurry", "closing": "Your 3 boxes are safe.",
                               "intro": "Understood, I kept speed first on this one."},
                         model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    prose = await compose.prose(ctx, rfq("en"), message())
    assert prose == compose.Prose(first_name="Anna", intro="Understood, I kept speed first on this one.")
    assert compose.stored(ctx, 1) == prose
    assert calls[0]["purpose"] == "quote.prose" and calls[0]["max_output_tokens"] == 150
    assert compose.customer_subject(ctx, 1) == "Machine parts to BBB"


async def test_prose_failure_never_blocks(ctx, monkeypatch):
    async def fail(ctx, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "ask_json", fail)
    assert await compose.prose(ctx, rfq("en"), message()) is None
    assert compose.stored(ctx, 1) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"first_name": "Anna Müller"}, "Anna"),
        ({"first_name": None}, None),
        ({"first_name": "  "}, None),
        ({"first_name": 42}, None),
        ({"first_name": "Lena"}, None),  # our own signer
        ({}, None),
    ],
)
def test_first_name_is_flexible_and_safe(raw, expected):
    assert compose.clean(raw, rfq("en"), message(), SIGNER).first_name == expected


@pytest.mark.parametrize("raw", [None, [], "text", {"intro": 7, "closing": ["x"]}, {"unexpected": "field"}])
def test_any_model_answer_gives_a_plain_full_mail(raw):
    prose = compose.clean(raw, rfq("en"), message(body=""), SIGNER)
    assert prose == compose.Prose()
    _, body = compose.render(rfq("en"), OPTION, prose)
    assert body.startswith("Hello,\n\n") and body.count("1234.57 EUR") == 1


def test_a_third_language_slot_is_dropped():
    raw = {"intro": "Merci pour votre demande, nous nous occupons de tout.", "closing": "Bonne journée."}
    assert compose.clean(raw, rfq("en"), message(), SIGNER) == compose.Prose()


def test_short_closings_in_both_languages_pass():
    assert compose.clean({"closing": "Have a good day"}, rfq("en"), message(), SIGNER).closing == "Have a good day"
    assert compose.clean({"closing": "Schönen Tag noch"}, rfq("de"), message(), SIGNER).closing


def test_a_rendering_error_with_slots_falls_back_to_plain(monkeypatch):
    real = compose._body

    def broken(rfq, option, prose, signer, *rest):
        if prose != compose.Prose():
            raise KeyError("boom")
        return real(rfq, option, prose, signer, *rest)

    monkeypatch.setattr(compose, "_body", broken)
    _, body = compose.render(rfq("en"), OPTION, compose.Prose(intro="Thanks for your request, happy to help."))
    assert body.count("1234.57 EUR") == 1


def test_odd_subjects_and_missing_customer_mail(ctx):
    assert compose.reply_subject("Quote\r\nBcc: x@y") == "Re: Quote Bcc: x@y"
    assert len(compose.reply_subject("x" * 500)) <= compose.SUBJECT_MAX + 4
    assert compose.reply_subject(None) is None and compose.customer_subject(ctx, 999) is None


def test_unreadable_stored_prose_is_none(ctx):
    ctx.db.execute(
        "INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', 5, 'quote.prose', 'rfq', 'x')"
    )
    ctx.db.execute(
        "INSERT INTO events (ts, session_id, kind, actor, payload) VALUES ('t', 6, 'quote.prose', 'rfq', '[1]')"
    )
    assert compose.stored(ctx, 5) is None and compose.stored(ctx, 6) is None and compose.stored(ctx, 7) is None


def test_adopt_never_raises(ctx, monkeypatch):
    monkeypatch.setattr(compose, "clean", lambda *a, **k: 1 / 0)
    assert compose.adopt(ctx, rfq("en"), message(), {"intro": "x"}) == compose.Prose()


def test_answers_the_arrival_from_the_ready_date_and_one_air_waybill():
    request = "Ready on 2026-11-25 at our site in 51447.\n\nLot 1: 300 kg\nLot 2: 200 kg\nOne air waybill please."
    pickup = OPTION.model_copy(update={"trucker": "Example Trucks", "pickup": Decimal(50)})
    _, body = compose.render(rfq("en"), pickup, request=request)
    assert "Wednesday 25 November" in body and "Sunday 29 November" in body  # 3 days transit, a day to the airport
    assert "single air waybill" in body and body.count("1234.57 EUR") == 1
    # Round 27 asked for "the earliest available pickup" too: answered as a pickup date, not only implied.
    _, body = compose.render(rfq("en"), pickup, request=request + " Please confirm the earliest available pickup.")
    assert "We can collect on Wednesday 25 November" in body and "Sunday 29 November" in body


def test_a_date_the_request_does_not_call_ready_gives_no_arrival():
    assert compose.ready_date("Please quote 300 kg, it must arrive by 2026-12-01.") is None


def test_the_mail_is_written_in_the_customers_register():
    request = "Dear Sir or Madam,\n\nplease quote AAA to BBB.\n\nKind regards\nPetra Hofmann\nHead of Logistics"
    option = OPTION.model_copy(update={"valid_until": datetime(2026, 11, 21, 16, 22, tzinfo=UTC)})
    _, body = compose.render(rfq("en"), option, request=request)
    # Round 45: "Dear Sir or Madam … Kind regards" got "Hi Anna! … Easy! 🙂". Named from the signature without a
    # model slot, formal to a formal mail, no exclamation mark, validity on our clock.
    assert body.startswith("Dear Petra Hofmann,\n\n") and "Kind regards,\nLena Berger\n" in body and "!" not in body
    assert "17:22 today" in body and "21.11.2026" not in body and "chargeable" not in body
    _, casual = compose.render(rfq("en", 2), option, request="Hi,\n\nquote AAA to BBB please.\n\nCheers,\nTom")
    # Their "Hi" and "Cheers" are answered in kind.
    assert casual.startswith("Hi Tom,\n\n") and "Cheers,\nLena Berger\n" in casual
    _, both = compose.render(
        rfq("en"), option, request="Hello,\n\nquote please.\n\nViele Gruesse / Many greetings\nDeniz"
    )
    assert "Viele Grüße,\nLena Berger\n" in both and "P.S." not in both


def test_the_customers_own_closing_is_answered_in_kind():
    """Rounds 12 to 16: every customer closes in a voice of their own; ours answered all of them alike."""
    request = (
        "Hi there,\n\nplease quote to BBB (Example City, XX), ready on 2026-11-25.\n\n"
        "Thanks a lot and have a lovely day!\nBest greetings from Exampletown\nPetra Hofmann\nExport Desk"
    )
    _, body = compose.render(rfq("en"), OPTION, request=request)
    assert "Have a lovely day too!\n\nBest regards to Exampletown,\nLena Berger\n" in body
    assert "arrival in Example City would be Saturday 28 November" in body and body.count("\n\n") == 5
    assert body.count("1234.57 EUR") == 1 and compose.check(body, rfq("en"), OPTION)
    german = "Guten Tag,\n\nbitte um ein Angebot.\n\nEin schönes Wochenende und Grüße aus Exampletown\nAnna Muster"
    _, body = compose.render(rfq("de"), OPTION, request=german)
    assert "Ihnen auch ein schönes Wochenende!\n\nViele Grüße nach Exampletown\nLena Berger\n" in body


OPENINGS = json.loads((Path(__file__).parent / "fixtures" / "openings.json").read_text())


@pytest.mark.parametrize("case", [c for c in OPENINGS if c["sent"]], ids=lambda c: str(c["session"]))
def test_no_opening_we_sent_live_would_pass_again(case):
    """Rounds 1 to 25: every opening that went out thanked for the request or recited the mail. None is better."""
    assert not compose._sentence_ok(case["sent"], "intro", "en", set())


def test_the_opening_answers_a_phrase_from_the_mail_without_its_names_and_varies_per_customer():
    raw = {"hook": "we are in a hurry", "intro": "Understood, I kept speed first on this one."}
    assert compose.clean(raw, rfq("en"), message(), SIGNER).intro
    assert not compose.clean({**raw, "hook": None}, rfq("en"), message(), SIGNER).intro
    assert not compose.clean({**raw, "hook": "you need it by Friday"}, rfq("en"), message(), SIGNER).intro
    small_talk = message("Hope your week's going well! Please quote AAA-BBB.\n\nAnna")
    raw_talk = {"hook": "Hope your week's going well", "intro": "Yes, a good week so far."}
    assert not compose.clean(raw_talk, rfq("en"), small_talk, SIGNER).intro
    named = {"hook": "we are in a hurry", "intro": "Understood Anna, I kept speed first."}
    assert not compose.clean(named, rfq("en"), message(), SIGNER).intro
    earlier = ("Understood, I will keep it moving.",)
    assert not compose.clean(raw, rfq("en"), message(), SIGNER, earlier=earlier).intro


async def test_the_model_sees_the_openings_this_customer_already_got(ctx, monkeypatch):
    calls = []

    async def fake_ask(ctx, **kw):
        calls.append(kw)
        return LlmResult(data={"first_name": None, "hook": "we are in a hurry", "closing": None,
                               "intro": "Understood, I kept speed first on this one."},
                         model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    for session in (1, 2):
        ctx.db.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (?, 'buyer@example.com', "
                       "'new', 't')", (session,))  # fmt: skip
    assert (await compose.prose(ctx, rfq("en", 1), message())).intro
    assert not (await compose.prose(ctx, rfq("en", 2), message())).intro
    assert "Understood, I kept speed first on this one." in calls[1]["user"]


def test_two_sessions_do_not_read_alike():
    bodies = {compose.render(rfq("en", session), OPTION)[1] for session in range(1, 9)}
    assert len(bodies) >= 5


def test_an_english_request_from_a_german_town_is_answered_in_english_throughout():
    """Round 11: "Lueneburg, Germany" made the model answer `de`; the quote opened in German, the dangerous-goods
    note was English, and the name was lost. Code decides the language, and every part of the mail follows it."""
    mail = captured("rfq-dg-instruction-bullets.json")
    request = extract.by_rules(mail, DEADLINE)
    assert request.language == "en"
    _, body = compose.render(request, OPTION, request=mail.body)
    body = dangerous.with_note(body, "UN1950, Class 2.1", compose.language_of(request), "packing-spec.pdf")
    assert body.startswith("Hello Sara,") and "shipper's declaration" in body
    assert not re.search(r"Guten Tag|Laufzeit|Grüße|Angebot|Gefahrgut", body)
    german = dangerous.with_note(compose.render(rfq("de"), OPTION)[1], "UN1950", "de", "x.pdf")
    assert "Gefahrgut" in german and "One point before we book" not in german and "bei 3 Tage" not in german


def test_a_forwarded_old_mail_below_the_request_gives_neither_name_nor_tone():
    """Round 14: "Hi there, … Petra Hofmann" above an old "-----Original Message----- … Dear team" went out as
    "Hello," with "Kind regards". Only the customer's own words count."""
    mail = captured("rfq-door-quoted-old-request.json")
    _, body = compose.render(rfq("en"), OPTION, request=mail.body)
    assert body.startswith("Hi Petra,") and "Best wishes," in body


def test_the_earliest_departure_is_answered_only_when_asked():
    asked = "Ready on 2026-11-25 at our site. Please confirm the earliest available departure and arrival."
    pickup = OPTION.model_copy(update={"trucker": "Example Trucks", "pickup": Decimal(50)})
    _, body = compose.render(rfq("en"), pickup, request=asked)
    assert "departure from AAA on Thursday 26 November" in body and "Sunday 29 November" in body
    _, body = compose.render(rfq("en"), pickup, request="Ready on 2026-11-25, truck to the departure airport please.")
    assert "departure from" not in body


@pytest.mark.parametrize(
    ("opening", "answered"),
    [
        ("Hope your week’s going well! Could you quote AAA to BBB?", True),
        ("Hope your week's going well. Please quote AAA to BBB.", True),
        ("I hope you're doing well. Please quote AAA to BBB.", True),
        ("Please quote AAA to BBB, hope the price is good as well.", False),
    ],
)
def test_the_customers_small_talk_is_answered_before_the_price(opening, answered):
    _, body = compose.render(rfq("en"), OPTION, request=f"Hi there,\n\n{opening}\n\nCheers,\nKlaus")
    paragraph = body.split("\n\n")[1]
    assert any(paragraph.startswith(w) for w in compose.TEXT["en"]["well_back"]) == answered
    assert paragraph.lower().count("thank") <= 1  # round 24: "Going well here, thanks! Thanks for your request"
    assert ("thank" in paragraph.lower()) == answered  # the opening no longer thanks: the small-talk answer does
    assert body.count("1234.57 EUR") == 1


def test_a_french_request_is_answered_in_french_with_its_fastest_option():
    """Round 70: a French mail asking for « l'option la plus rapide » was read as English and cheapest, and the
    English quote was rejected as wrong_language."""
    mail = captured("rfq-french-fastest.json")
    request = extract.by_rules(mail, DEADLINE)
    assert (request.language, request.preference) == ("fr", extract.Preference.FASTEST)
    option = OPTION.model_copy(update={"origin": "DUS", "destination": "SYD", "chargeable_kg": Decimal("1412.49")})
    _, body = compose.render(request, option, request=mail.body)
    assert body.startswith("Bonjour Klaus,") and "tout compris" in body and "3 jours" in body
    assert "1412,49 kg" in body and "le jeudi 26 novembre" in body and "À vous aussi, bonne journée !" in body
    assert compose.check(body, request, option) and not re.search(r"\b(?:Hello|Hi|days|transit is|regards)\b", body)
    note = dangerous.with_note(body, "UN1950", "fr", "mail")
    assert "marchandises dangereuses" in note
    counts = compose._words_lang(note)
    assert counts["fr"] > 2 * max(counts["en"], counts["de"])


@pytest.mark.parametrize(
    ("request_text", "goods"),
    [
        (
            "Could you quote the fastest air freight option for some electronic control units going to SCL?",
            "electronic control units",
        ),  # round 62 sent "For your some electronic control units going"
        ("Could you quote the cheapest option for our machine parts going to JFK?", "machine parts"),
        ("We have 10 pieces of laboratory glassware ready on Monday.", "laboratory glassware"),
    ],
)
def test_the_goods_are_named_as_the_customer_calls_them(request_text, goods):
    assert compose.goods_of(request_text) == goods


async def test_a_request_in_a_language_we_have_no_text_for_is_written_by_the_model_and_checked_by_code(
    ctx, monkeypatch
):
    """Round 72: a Chinese mail. Code's word count read it as English, the template went out in English and was
    rejected as wrong_language, although the model had read it as Chinese."""
    ctx.db.execute("INSERT INTO carriers VALUES ('Other Air', 'airline', 'o@air', 'BBB')")
    mail = captured("rfq-chinese-door.json")
    assert extract._language(extract._text(mail), "zh") == "zh"
    request = rfq("zh").model_copy(update={"door_pickup": True, "pickup_postcode": "50978"})
    option = OPTION.model_copy(update={"trucker": "Example Trucking", "pickup": Decimal("112.28")})
    written = {"body": ""}

    async def model(ctx, **kw):
        assert "Price: 1234.57 EUR all-in (pickup from the customer's site by Example Trucking" in kw["user"]
        return LlmResult(data=written, model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))

    monkeypatch.setattr(llm, "ask_json", model)
    written["body"] = "Petra 您好，\n\nExample Air 从 AAA 到 BBB：1234.57 EUR 全包，运输时间 3 天，704 kg。\n\n此致"
    body = await compose.letter(ctx, request, option, mail.body, None, 5)
    assert body.startswith("Petra 您好") and body.endswith(f"{SIGNER}\n{compose.COMPANY}")
    for wrong in ("1234.57 EUR，另加 112.28 EUR", "1234.57 EUR 和 1234.57 EUR", "Other Air 1234.57 EUR"):
        written["body"] = f"Example Air 3 天 {wrong}"
        assert await compose.letter(ctx, request, option, mail.body, None, 5) is None
    written["body"] = "Example Air 1234.57 EUR 3 天"
    assert await compose.letter(ctx, request, option, mail.body, "UN1950", 5) is None  # no DGD, no MSDS
