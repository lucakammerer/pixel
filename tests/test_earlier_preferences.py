import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pixel import llm
from pixel.agents.rfq import extract
from pixel.config import settings
from pixel.contracts import Attachment, Message, Preference, Rfq
from pixel.core.context import Context
from pixel.core.orchestrator import deadline_for

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "rfqs" / "earlier-preference.json").read_text())
AT = datetime(2026, 11, 22, 10, tzinfo=UTC)
BUYER = "buyer@example.com"


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
async def test_the_new_request_chooses_its_own_option_when_reusing_earlier_cargo(conn, monkeypatch, case):
    config = replace(settings(), model_check="")
    deadline = deadline_for(AT, config)
    earlier = Rfq(
        session_id=1, message_id=1, customer=BUYER, received_at=AT, deadline_at=deadline.expires_at,
        **FIXTURE["earlier"],
    )  # fmt: skip
    conn.execute(
        "INSERT INTO sessions (id, customer, state, received_at) VALUES (1, ?, ?, ?)",
        (BUYER, case.get("earlier_state", "quoted"), AT.isoformat()),
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 1, 'in', 'customer_rfq', NULL, ?, 'us@example.com', ?, '', NULL, ?, '{}')",
        (BUYER, case["old_subject"], AT.isoformat()),
    )
    conn.execute(
        "INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, 1, 'rfq.extracted', 'rfq', ?)",
        (AT.isoformat(), earlier.model_dump_json()),
    )
    message = Message(
        id=2, session_id=2, direction="in", sender=case.get("sender", BUYER), to="us@example.com",
        subject="A fresh request", sent_at=AT,
        body=f'Please quote the same shipment as in my earlier request, “{case["old_subject"]}”.\n{case["current"]}',
    )  # fmt: skip
    context = Context(config, conn, None, None, None, None)
    if case["prefer"] is None:
        assert extract._earlier(context, message, deadline) is None
        return

    if content := case.get("attachment"):
        attachment = Attachment(id=1, filename="instructions.txt", content_type="text/plain", size=len(content))

        async def files(ctx, mail):
            return [(attachment.filename, [content])]

        monkeypatch.setattr(extract, "_attachments", files)
        message = message.model_copy(update={"attachments": (attachment,)})

    async def no_model(*args, **kwargs):
        pytest.fail("the recorded shipment and written instructions suffice to read the new request")

    monkeypatch.setattr(llm, "ask_json", no_model)
    read, _ = await extract.read_request(context, message, deadline)
    assert (read.origin, read.destination, read.weight_kg, read.volume_cbm, read.pieces) == (
        earlier.origin,
        "CCC",
        Decimal(FIXTURE["earlier"]["weight_kg"]),
        Decimal(FIXTURE["earlier"]["volume_cbm"]),
        earlier.pieces,
    )
    if case.get("known_gap") and read.preference == Preference.CHEAPEST:
        pytest.xfail(case["known_gap"])
    assert read.preference == Preference(case["prefer"])
