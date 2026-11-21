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
    assert not open_now(conn, *at(310))  # the next request is due within a minute
    add(conn, 1, "route", T0 + timedelta(seconds=130), '{"kind": "carrier_bid"}')  # a bid is not a request
    assert open_now(conn, *at(135))


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
