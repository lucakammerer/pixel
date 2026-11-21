from datetime import UTC, datetime, timedelta

from tools.window import open_now

T0 = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


def add(conn, session_id, kind, at, payload="{}"):
    conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, ?, ?, 'x', ?)",
                 (at.isoformat(), session_id, kind, payload))  # fmt: skip


def test_the_window_opens_with_our_answer_and_closes_before_the_next_request(conn):
    assert open_now(conn, T0)  # nothing asked yet
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    assert not open_now(conn, T0 + timedelta(seconds=20))  # the request is still being answered
    add(conn, 1, "quote.sent", T0 + timedelta(seconds=30))
    assert open_now(conn, T0 + timedelta(seconds=40))
    assert not open_now(conn, T0 + timedelta(seconds=310))  # the next request is due within a minute
    add(conn, 1, "route", T0 + timedelta(seconds=45), '{"kind": "carrier_bid"}')  # a bid is not a request
    assert open_now(conn, T0 + timedelta(seconds=50))


def test_deploy_margin_uses_stored_request_time_even_when_routing_was_late(conn):
    conn.execute(
        "INSERT INTO sessions(id, customer, state, received_at) VALUES (1, 'buyer', 'quoted', ?)", (T0.isoformat(),)
    )
    add(conn, 1, "route", T0 + timedelta(seconds=70), '{"kind":"customer_rfq"}')
    add(conn, 1, "quote.sent", T0 + timedelta(seconds=80))
    assert not open_now(conn, T0 + timedelta(seconds=350))
