import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pixel import llm
from pixel.config import settings
from pixel.core import events
from pixel.core.context import Context


class FakeGateway:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat(self, payload):
        self.calls.append(payload)
        return self.response


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda ctx, kind, actor, **kw: out.append((kind, kw)))
    return out


def ctx_with(conn, response):
    return Context(settings=settings(), db=conn, mailbox=None, portal=None, gateway=FakeGateway(response), board=None)


def log_spend(conn, session_id, cost, ts):
    conn.execute(
        "INSERT INTO events (ts, session_id, kind, actor, payload, cost_usd) VALUES (?, ?, 'llm.call', 'llm', '{}', ?)",
        (ts.isoformat(), session_id, cost),
    )


async def test_ask_json_parses_and_emits(conn, emitted):
    response = {
        "choices": [{"message": {"content": json.dumps({"origin": "AAA"})}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8, "cost": "0.000031"},
    }
    ctx = ctx_with(conn, response)
    result = await llm.ask_json(ctx, purpose="rfq extract", system="s", user="u", schema={}, session_id=5)
    assert result.data == {"origin": "AAA"}
    assert (result.tokens_in, result.tokens_out, result.cost_usd) == (120, 8, Decimal("0.000031"))
    kind, kw = emitted[0]
    assert kind == "llm.call" and kw["session_id"] == 5 and kw["cost_usd"] == Decimal("0.000031")
    assert ctx.gateway.calls[0]["response_format"]["json_schema"]["name"] == "rfq_extract"


async def test_budget_exceeded_does_not_call(conn, emitted):
    log_spend(conn, 9, str(llm.SESSION_BUDGET_USD), datetime.now(UTC))
    ctx = ctx_with(conn, {})
    with pytest.raises(llm.BudgetExceeded):
        await llm.ask_json(ctx, purpose="p", system="s", user="u", schema={}, session_id=9)
    assert ctx.gateway.calls == [] and emitted == []


def test_spend_per_session_and_window(conn):
    now = datetime.now(UTC)
    log_spend(conn, 1, "0.001", now)
    log_spend(conn, 1, "0.002", now - timedelta(hours=2))
    log_spend(conn, 2, "0.004", now)
    ctx = ctx_with(conn, {})
    assert llm.spend_usd(ctx, session_id=1) == Decimal("0.003")
    assert llm.spend_usd(ctx, last_minutes=60) == Decimal("0.005")


def test_gateway_exception_contains_only_safe_status():
    import httpx

    from pixel.api.client import ApiError
    from pixel.api.gateway import _checked

    with pytest.raises(ApiError) as error:
        _checked(httpx.Response(403, text="untrusted provider response body"))
    assert error.value.status == 403 and "untrusted" not in str(error.value)
