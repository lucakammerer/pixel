"""Wait for the moment a deploy cannot cost a request: the current request is over two minutes old, our answer
to it is out (a miss counts only once it is past `hard_at`, where the fallback stops looking for a price), and the
next request is not due within 30 s. A window that closed before `hard_at` stayed shut whenever every request went
out as the fallback, and so did the fix for it (rounds 63 to 65). With no request for more than a round, it is open.
`--urgent` (a merge with URGENT in its title) skips the first two minutes: the window opens as soon as the answer is
out. Exit 0 when that moment is now, 1 when it did not come within 5 minutes.

uv run python -m tools.window            # on the live host; tools/deploy.sh --after-quote calls it
uv run python -m tools.window --urgent   # deploy/cd.sh for an URGENT merge
"""

import sqlite3
import sys
import time
from datetime import UTC, datetime

from pixel.config import settings

ROUND_S = 360  # a request every 6 minutes (R1)
MARGIN_S = 60  # a late next request: no request for a round and this long, the window is open
NEXT_S = 30  # keep clear of the next request; the fallback goes out at hard_at (315 s), a restart takes seconds
FIRST_S = 120  # no deploy in a request's first two minutes


def open_now(conn: sqlite3.Connection, now: datetime, hard_s: int, first_s: int = FIRST_S) -> bool:
    asked = conn.execute(
        "SELECT e.session_id, COALESCE(m.sent_at, s.received_at, e.ts) AS received "
        "FROM events e LEFT JOIN messages m ON m.id = COALESCE(e.message_id, e.session_id) "
        "LEFT JOIN sessions s ON s.id = e.session_id "
        "WHERE e.kind = 'route' AND json_extract(e.payload, '$.kind') = 'customer_rfq' "
        "ORDER BY received DESC LIMIT 1"
    ).fetchone()
    if asked is None:
        return True
    age = (now - datetime.fromisoformat(asked[1])).total_seconds()
    if age >= ROUND_S + MARGIN_S:
        return True
    if not first_s <= age < ROUND_S - NEXT_S:
        return False
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM events WHERE session_id = ?", (asked[0],))}
    # A miss is no answer: the fallback keeps looking for a price until hard_at, and a restart drops that search.
    return bool(kinds & {"quote.sent", "quote.fallback"}) or ("quote.missed" in kinds and age >= hard_s)


def main() -> None:
    conn = sqlite3.connect(f"file:{settings().db_path}?mode=ro", uri=True)
    first_s = 0 if "--urgent" in sys.argv[1:] else FIRST_S
    for _ in range(100):
        if open_now(conn, datetime.now(UTC), settings().quote_hard_s, first_s):
            return
        time.sleep(3)
    sys.exit(1)


if __name__ == "__main__":
    main()
