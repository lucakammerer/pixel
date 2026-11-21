import base64
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from pixel.agents.rfq import extract
from pixel.config import settings
from pixel.contracts import Attachment, Message
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for
from pixel.pricing import store
from tools import replay


@pytest.fixture
def ctx(conn):
    return Context(settings(), conn, None, None, None, None)


def request(body, names=()):
    return Message(
        id=17,
        session_id=17,
        direction="in",
        sender="buyer@example.org",
        to="team@example.org",
        subject="Shipment enquiry",
        sent_at=datetime.now(UTC),
        body=body,
        attachments=tuple(Attachment(id=i, filename=n, content_type="", size=0) for i, n in enumerate(names)),
    )


def scan(fmt):
    stream = io.BytesIO()
    page = Image.new("RGB", (32, 32), "white")
    options = {"save_all": True, "append_images": [page, page]} if fmt == "PDF" else {}
    page.save(stream, format=fmt, **options)
    return stream.getvalue()


TABLE = [
    "Gross weight: 65 kg",
    "Total volume: 1.2 cbm",
    "Pieces  Dimensions (cm)  Gross weight (kg)",
    "2  100 x 50 x 100  40",
    "1  100 x 20 x 100  25",
]


@pytest.mark.parametrize("suffix,fmt,page_count", [("pdf", "PDF", 2), ("PNG", "PNG", 1), ("JpEg", "JPEG", 1)])
async def test_scan_uses_one_call_and_corroborated_measurements(ctx, monkeypatch, suffix, fmt, page_count):
    name = f"packing.{suffix}"
    mail = request("AAA to BBB. Non-dangerous goods. 50 kg, 3 pieces, 2 cbm.", [name])
    ctx.mailbox = SimpleNamespace(attachment=AsyncMock(return_value=scan(fmt)))
    pages = [{"lines": TABLE, "dangerous_goods": "Class 3"}]
    if page_count == 2:
        pages = [{"lines": TABLE[:2], "dangerous_goods": None}, {"lines": TABLE[2:], "dangerous_goods": "Class 3"}]
    gateway = AsyncMock(return_value=SimpleNamespace(data={"image_pages": pages}))
    monkeypatch.setattr(extract.llm, "ask_json", gateway)
    rfq, _ = await extract.read_request(ctx, mail, deadline_for(mail.sent_at, ctx.settings))
    gateway.assert_awaited_once()
    images = gateway.await_args.kwargs["images"]
    assert len(images) == page_count and all(Image.open(io.BytesIO(image)).format == "JPEG" for image in images)
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.missing) == (65, Decimal("1.2"), (extract.DG_DOCUMENTS,))
    assert (
        ctx.db.execute(
            "SELECT json_extract(payload, '$.source') FROM events WHERE kind='rfq.dangerous_goods'"
        ).fetchone()[0]
        == name
    )


async def test_mixed_attachments_limit_and_gateway_outage(ctx, monkeypatch):
    mail = request("AAA to BBB. 40 kg, 1 cbm.", ["p.jpg", "q.jpeg", "r.png", "instruction.txt"])
    ctx.mailbox = SimpleNamespace(
        attachment=AsyncMock(side_effect=[scan("JPEG"), scan("JPEG"), b"Dangerous goods: Class 3"])
    )
    gateway = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(extract.llm, "ask_json", gateway)
    rfq = await extract.extract(ctx, mail, deadline_for(mail.sent_at, ctx.settings))
    assert ctx.mailbox.attachment.await_count == 3
    assert len(gateway.await_args.kwargs["images"]) == 2
    assert (rfq.weight_kg, rfq.volume_cbm, rfq.extracted_by, rfq.missing) == (40, 1, "guess", (extract.DG_DOCUMENTS,))


@pytest.mark.parametrize(
    "header,row",
    [
        ("Bruttogewicht (kg)  Abmessungen (mm)", "80  1200 × 800 × 1000"),
        ("Dimensions (m)  Gross weight (kg)", "1,2 x 0,8 x 1  80"),
        ("Abmessungen (cm)  Stück", "120 x 80 x 100  1"),
    ],
)
def test_combined_dimensions_need_agreeing_totals(header, row):
    text = f"Bruttogewicht: 80 kg\nTotal volume: 0.96 cbm\n{header}\n{row}"
    mail = "AAA to BBB, 50 kg, 1.8 cbm."
    facts, _ = extract._cargo_sources([("mail", mail), ("packing", text)])
    assert facts["weight_kg"] == (80 if "(kg)" in header else 50)
    assert facts["volume_cbm"] == Decimal("0.96")
    facts, _ = extract._cargo_sources(
        [("mail", mail), ("packing", text.replace("0.96 cbm", "4 cbm").replace("80 kg", "99 kg"))]
    )
    assert (facts["weight_kg"], facts["volume_cbm"]) == (50, Decimal("1.8"))


async def test_offline_replay_reaches_rules_fallback_for_image():
    fixture = Path(__file__).parent / "fixtures" / "mails" / "rfq-port-to-port.json"
    raw = json.loads(fixture.read_bytes())
    image = scan("PNG")
    raw["attachments"] = [
        {
            "id": 1,
            "filename": "packing.png",
            "content_type": "image/png",
            "size": len(image),
            "content_base64": base64.b64encode(image).decode(),
        }
    ]
    events = await replay.run([raw], online=False, as_live=True)
    assert any(e.kind == "rfq.extracted" and e.payload["extracted_by"] == "guess" for e in events)


def test_per_piece_header_does_not_corroborate_a_gross_total():
    doc = "Gross weight: 50 kg\nPieces  Dimensions (cm)  Gross weight (kg) per piece\n3  100 x 100 x 100  50"
    assert extract._document_weight(doc) is None


async def test_oversized_image_is_skipped_before_download(ctx, monkeypatch):
    mail = request("AAA to BBB, 10 kg, 1 cbm.", ["large.jpg"])
    mail = mail.model_copy(
        update={"attachments": (mail.attachments[0].model_copy(update={"size": extract.IMAGE_BYTES + 1}),)}
    )
    ctx.mailbox = SimpleNamespace(attachment=AsyncMock())
    gateway = AsyncMock()
    monkeypatch.setattr(extract.llm, "ask_json", gateway)
    rfq = await extract.extract(ctx, mail, deadline_for(mail.sent_at, ctx.settings))
    ctx.mailbox.attachment.assert_not_awaited()
    gateway.assert_not_awaited()
    assert rfq.weight_kg == 10


async def test_bare_town_reply_on_model_path(ctx, monkeypatch):
    mail = request("Collect the equipment at our warehouse for onward export.")
    store.save_zones(
        ctx,
        [{"area": "Sampletown", "zone": "12", "airport": "AAA"}, {"area": "Elsewhere", "zone": "34", "airport": "CCC"}],
    )
    gateway = AsyncMock(
        return_value=SimpleNamespace(
            data={
                "destination": "BBB",
                "weight_kg": "45",
                "volume_cbm": "1",
                "door_pickup": True,
            }
        )
    )
    monkeypatch.setattr(extract.llm, "ask_json", gateway)
    question = AsyncMock(return_value="Sampletown\nKind regards\nElsewhere\n> Previous request")
    monkeypatch.setattr(extract, "_ask", question)
    rfq = await extract.extract(ctx, mail, deadline_for(mail.sent_at, ctx.settings))
    question.assert_awaited_once()
    assert (rfq.pickup_zone, rfq.origin, rfq.missing, rfq.extracted_by) == ("12", "AAA", (), "model")


async def test_circuit_breaker_skips_question(ctx, monkeypatch):
    mail = request("AAA to BBB, 10 kg.")
    send = AsyncMock()
    monkeypatch.setattr(extract.outbox, "send", send)
    monkeypatch.setattr(extract.feedback, "ask_allowed", lambda ctx: False, raising=False)
    rfq = await extract.extract(ctx, mail, deadline_for(mail.sent_at, ctx.settings))
    send.assert_not_awaited()
    assert rfq.weight_kg == 10


@pytest.mark.parametrize(
    "words,expected",
    [
        ("Please quote in US dollars.", "USD"),
        ("All-in price in $.", "USD"),
        ("Currency: £", "GBP"),
        ("Currency: USD\n2 cartons", "USD"),
        ("Price: GBP", "GBP"),
        ("Price in Swiss francs", "CHF"),
        ("Currency CHF", "CHF"),
        ("Our invoice is in GBP, but quote in USD.", "USD"),
        ("Declared value $200 or £160.\nRegards,\nPrice: USD", None),
        ("Declared value in $.\n> Please quote in Swiss francs.", None),
        ("Please quote in £.\nOn Monday a buyer wrote:\nPrice: CHF", "GBP"),
    ],
)
def test_requested_currency(words, expected):
    mail = request(f"AAA to BBB, 10 kg, 1 cbm.\n{words}")
    assert extract.by_rules(mail, deadline_for(mail.sent_at, settings())).currency == expected
