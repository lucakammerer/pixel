from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pixel import llm
from pixel.agents.rfq import compose
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


def test_pickup_is_named():
    option = OPTION.model_copy(update={"trucker": "Example Trucks", "pickup": Decimal(50)})
    _, body = compose.render(rfq("en"), option)
    assert "Example Trucks" in body


def test_reply_subject_name_and_signer():
    prose = compose.Prose(first_name="Anna", intro="Happy to help get the machine parts moving quickly.",
                          closing="Have a good day.")  # fmt: skip
    subject, body = compose.render(rfq("en"), OPTION, prose, "Machine parts to BBB", SIGNER)
    assert subject == "Re: Machine parts to BBB"
    assert body.startswith("Hello Anna,\n\nHappy to help get the machine parts moving quickly.")
    assert "Have a good day." in body and body.endswith(f"{SIGNER}\n{compose.COMPANY}")
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
    raw = {"first_name": "Anna", "intro": "Thanks for the details on the machine parts, we will keep it quick.",
           "closing": "Have a good afternoon."}  # fmt: skip
    assert compose.clean(raw, rfq("en"), message(), SIGNER) == compose.Prose(**raw)
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
        return LlmResult(data={"first_name": "Anna", "intro": "Happy to keep your machine parts moving fast.",
                               "closing": "Your 3 boxes are safe with us."},
                         model="m", tokens_in=1, tokens_out=1, cost_usd=Decimal(0))  # fmt: skip

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    prose = await compose.prose(ctx, rfq("en"), message())
    assert prose == compose.Prose(first_name="Anna", intro="Happy to keep your machine parts moving fast.")
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
    _, body = compose.render(rfq("en"), OPTION, compose.Prose(closing="Have a good day"))
    assert "Have a good day." in body


def test_a_rendering_error_with_slots_falls_back_to_plain(monkeypatch):
    real = compose._body

    def broken(rfq, option, prose, signer):
        if prose != compose.Prose():
            raise KeyError("boom")
        return real(rfq, option, prose, signer)

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
