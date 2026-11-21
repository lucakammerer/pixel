"""Wait for the moment a deploy cannot cost a request: our answer to the current request is out and the next
request is not due yet. Exit 0 when that moment is now, 1 when it did not come within 5 minutes.

uv run python -m tools.window        # on the live host; tools/deploy.sh --after-quote calls it
"""

import sqlite3
import sys
import time
from datetime import UTC, datetime

from pixel.config import settings

ROUND_S = 360  # a request every 6 minutes (R1)
MARGIN_S = 60  # keep clear of the next request


def open_now(conn: sqlite3.Connection, now: datetime) -> bool:
    asked = conn.execute(
        "SELECT session_id, ts FROM events WHERE kind = 'route' AND json_extract(payload, '$.kind') = 'customer_rfq'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if asked is None:
        return True
    answered = conn.execute(
        "SELECT 1 FROM events WHERE session_id = ? AND kind IN ('quote.sent', 'quote.fallback', 'quote.missed')",
        (asked[0],),
    ).fetchone()
    return bool(answered) and (now - datetime.fromisoformat(asked[1])).total_seconds() < ROUND_S - MARGIN_S


def main() -> None:
    conn = sqlite3.connect(f"file:{settings().db_path}?mode=ro", uri=True)
    for _ in range(100):
        if open_now(conn, datetime.now(UTC)):
            return
        time.sleep(3)
    sys.exit(1)


if __name__ == "__main__":
    main()
