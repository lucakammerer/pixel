from datetime import UTC, datetime

import pytest

from pixel.agents.rfq.compose import NO_PRICE, OUT_OF_SCOPE
from pixel.contracts import Event
from tools.scope_cases import outcome
from tools.scope_proof import scope_outcome


@pytest.mark.parametrize(
    ("body", "expected"),
    [(OUT_OF_SCOPE["de"], "scope decline"), (NO_PRICE["de"], "unpriced")],
)
def test_scope_proof_distinguishes_decline_from_missing_rate_after_a_question(body, expected):
    customer = "customer@example.com"
    now = datetime.now(UTC)
    history = [
        Event(ts=now, actor="outbox", kind="mail.out", payload={"to": customer, "purpose": "question"}),
        Event(ts=now, actor="outbox", kind="mail.out", payload={"to": customer, "purpose": "no_price", "body": body}),
    ]
    assert scope_outcome(history, customer, outcome(history, customer)) == expected
