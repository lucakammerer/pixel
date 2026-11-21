from datetime import UTC, datetime, timedelta

from tools.window import open_now

T0 = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
HARD_S = 315


def add(conn, session_id, kind, at, payload="{}"):
    conn.execute("INSERT INTO events (ts, session_id, kind, actor, payload) VALUES (?, ?, ?, 'x', ?)",
                 (at.isoformat(), session_id, kind, payload))  # fmt: skip


def at(s, hard_s=HARD_S):
    return T0 + timedelta(seconds=s), hard_s


def test_the_window_opens_after_our_answer_and_the_first_two_minutes_and_closes_before_the_next_request(conn):
    assert open_now(conn, *at(0))  # nothing asked yet
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    assert not open_now(conn, *at(20))  # the request is still being answered
    add(conn, 1, "quote.sent", T0 + timedelta(seconds=30))
    assert not open_now(conn, *at(40))  # answered, but still in the first two minutes
    assert open_now(conn, *at(125))
    assert not open_now(conn, *at(335))  # the next request is due within 30 s
    add(conn, 1, "route", T0 + timedelta(seconds=130), '{"kind": "carrier_bid"}')  # a bid is not a request
    assert open_now(conn, *at(135))


def test_a_fallback_at_the_hard_deadline_still_opens_the_window(conn):
    """Rounds 63 to 65: every quote went out as the fallback at 315 s; a window that ended at 300 s kept the fix out."""
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    add(conn, 1, "quote.fallback", T0 + timedelta(seconds=HARD_S))
    assert open_now(conn, *at(HARD_S + 3))


def test_an_urgent_deploy_goes_as_soon_as_the_answer_is_out(conn):
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    assert not open_now(conn, *at(10), first_s=0)  # the request is still being answered
    add(conn, 1, "quote.sent", T0 + timedelta(seconds=30))
    assert open_now(conn, *at(32), first_s=0)
    assert not open_now(conn, *at(32))


def test_a_missed_request_keeps_the_window_shut_while_it_can_still_get_a_quote(conn):
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    add(conn, 1, "quote.missed", T0 + timedelta(seconds=2), '{"reason": "unreadable"}')
    assert not open_now(conn, *at(11))  # round 17: the miss is no answer
    assert not open_now(conn, *at(200))
    assert not open_now(conn, *at(150, hard_s=200))
    assert open_now(conn, *at(200, hard_s=200))  # past hard_at nothing looks for a price any more


def test_with_no_request_for_more_than_a_round_the_window_is_open(conn):
    add(conn, 1, "route", T0, '{"kind": "customer_rfq"}')
    add(conn, 1, "quote.missed", T0 + timedelta(seconds=315), '{"reason": "no_price"}')
    assert not open_now(conn, *at(380))  # the next request may just be late
    assert open_now(conn, *at(425))


def test_deploy_margin_uses_stored_request_time_even_when_routing_was_late(conn):
    conn.execute(
        "INSERT INTO sessions(id, customer, state, received_at) VALUES (1, 'buyer', 'quoted', ?)", (T0.isoformat(),)
    )
    add(conn, 1, "route", T0 + timedelta(seconds=70), '{"kind":"customer_rfq"}')
    add(conn, 1, "quote.sent", T0 + timedelta(seconds=80))
    assert not open_now(conn, *at(119))
    assert open_now(conn, *at(120))
    assert not open_now(conn, *at(350))
    assert open_now(conn, *at(420))
