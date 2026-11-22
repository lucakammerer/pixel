"""Outage drills: does a quote within 5 % of the scorer's best still leave before the deadline when one dependency
fails? Offline, on the accepted rounds of a database, each replayed on the state and portal answers it met
(`tools/capture.py`).

uv run python -m tools.outage_drill <database>                          # the last 30 accepted rounds, every drill
uv run python -m tools.outage_drill <database> --last 60 --drill portal_down

The answer per round does not come from the code under test: the scorer accepted the live quote, so its best price
is `feedback.best` where the scorer's points explain it, else within 5 % of the live total. A cell is right when a
quote leaves within 6 minutes of the request's storage and within 5 % of that price. Offline every model call fails:
each drill is also one of the gateway down, and `gateway_down` alone is the baseline. A failing cell names the round
and what left instead; it is a known gap in `bench/NEXT_CASES.md` with its owner and cause.
"""

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal

import httpx

from pixel.api.mailbox import RATEDESK
from pixel.contracts import Event
from tools import capture, replay

BAND = capture.BAND
DEADLINE_S = 360  # R1: a request expires 6 minutes after it was stored
LATE_S = 90  # `mail_late`: each mail is seen this long after it was stored
GATEWAY_TIMEOUT_S = 60  # `pixel/api/gateway.py`
RATE_TIMEOUT_S = 12  # past `portal.READ_TIMEOUT_S`


def _hang(seconds: float):
    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        raise httpx.ReadTimeout("drill: no answer", request=request)

    return answer


def _api(fault: Callable[[httpx.Request], object] | None, prefix: str):
    """The recorded portal, with `fault` answering every call whose path starts with `prefix`."""

    def wrap(portal: dict):
        recorded = replay._recorded(portal)

        def answer(request: httpx.Request):
            return fault(request) if fault and request.url.path.startswith(prefix) else recorded(request)

        return answer

    return wrap


def _status(code: int, **headers: str):
    return lambda request: httpx.Response(code, headers=headers, json={"detail": "drill"})


# name: (the API, what of the session's mails arrives, seconds late)
DRILLS: dict[str, tuple] = {
    "gateway_down": (_api(None, ""), "all", 0),
    "gateway_hangs": (_api(_hang(GATEWAY_TIMEOUT_S), "/v1/gateway"), "all", 0),
    "portal_down": (_api(_status(503), "/v1/portal/"), "all", 0),
    "portal_429": (_api(_status(429, **{"Retry-After": "60"}), "/v1/portal/rates"), "all", 0),
    "portal_nonsense": (_api(lambda r: httpx.Response(200, json={"data": None}), "/v1/portal/"), "all", 0),
    "portal_hangs": (_api(_hang(RATE_TIMEOUT_S), "/v1/portal/rates"), "all", 0),
    "mail_twice": (_api(None, ""), "twice", 0),
    "mail_late": (_api(None, ""), "all", LATE_S),
    "airlines_silent": (_api(None, ""), "airline", 0),
    "truckers_silent": (_api(None, ""), "trucker", 0),
}


def mails(raws: list[dict], state: dict, which: str) -> list[dict]:
    """The session's mails as the drill delivers them: all, each twice, or without the airlines' and rate desk's or
    the truckers' answers."""
    if which == "twice":
        return [raw for raw in raws for _ in range(2)]
    if which == "all":
        return raws
    silent = {c["email"].lower() for c in state["carriers"] if c["kind"] == which} | (
        {RATEDESK} if which == "airline" else set()
    )
    return [raw for raw in raws if raw.get("from", "").lower() not in silent]


def outcome(events: list[Event], late_s: float, nth: int) -> tuple[Decimal | None, float | None]:
    """The scored quote's total in EUR and its seconds from storage; (None, None) when none left. A requote the scorer
    scored is our last quote when an earlier one was held as superseded (a change before the first quote left)."""
    nth = min(nth, max(sum(e.kind == "quote.sent" for e in events) - 1, 0))
    quote = capture.quoted(events, nth)
    sent = next(iter([e for e in events if e.kind == "quote.sent"][nth:]), None)
    arrived = next((e for e in events if e.kind == "mail.in"), None)
    if quote is None or sent is None or arrived is None:
        return None, None
    return quote["total_eur"], (sent.ts - arrived.ts).total_seconds() + late_s


def verdict(total: Decimal | None, seconds: float | None, best: Decimal) -> str | None:
    """None when right, else what went wrong."""
    if total is None:
        return "no quote"
    if seconds > DEADLINE_S:
        return f"late, {seconds:.0f} s"
    if abs(total - best) > best * BAND:
        return f"{total} against {best}"
    return None


async def drill(raws: list[dict], state: dict, name: str, nth: int = 0) -> tuple[Decimal | None, float | None]:
    api, which, late_s = DRILLS[name]
    events = await replay.run(
        mails(raws, state, which), False, replay.seeded(state), True, api(state["portal"]), late_s
    )
    return outcome(events, late_s, nth)


def today(conn: sqlite3.Connection, session_id: int, state: dict) -> dict:
    """The session's state with the rate card as it is now: every portal rate of its lanes, its own reads too, as
    events of no session. The card's job reads every bracket of every portal lane (by 22.11. 10:00 all 1341 of 206
    lane and airline pairs), so it holds the rates the session read live; a drill asks whether a request like this
    one is answered when the portal fails today."""
    lanes = {(p["origin"], p["destination"]) for p in capture._payloads(
        conn, "session_id = ? AND kind = 'rfq.extracted'", (session_id,))}  # fmt: skip
    card = [
        {k: e[k] for k in capture.EVENT} | {"session_id": None}
        for e in conn.execute(
            "SELECT * FROM events WHERE kind = 'rate_card.read' OR (kind = 'option.found' AND"
            " json_extract(payload, '$.option.source') = 'portal')"
        )
        if ((p := json.loads(e["payload"]).get("option") or json.loads(e["payload"]))["origin"], p["destination"])
        in lanes
    ]
    known = {e["id"] for e in state["events"]}
    events = sorted(state["events"] + [e for e in card if e["id"] not in known], key=lambda e: e["id"])
    return state | {"events": events}


def accepted(conn: sqlite3.Connection, last: int) -> list[tuple[int, int, Decimal, int]]:
    """(round, session, the scorer's best or our accepted total, which of our quotes it scored: a requote answers a
    changed request) of the newest accepted requests, newest first."""
    rows = conn.execute(
        "SELECT session_id, payload FROM events WHERE kind = 'feedback' AND session_id IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    found, seen = [], set()
    for session_id, payload in rows:
        p = json.loads(payload)
        if p.get("verdict") != "accepted" or session_id in seen:
            continue
        seen.add(session_id)
        sent = [json.loads(q)["total"] for (q,) in conn.execute(
            "SELECT payload FROM events WHERE kind = 'quote.sent' AND session_id = ? ORDER BY id", (session_id,))]  # fmt: skip
        nth = next(
            (i for i, total in enumerate(sent) if Decimal(str(total)) == Decimal(str(p.get("quoted") or "NaN"))), 0
        )
        live = capture.live(conn, session_id)
        best = Decimal(str(p["best"])) if p.get("best") is not None else live and live["total_eur"]
        if best:
            found.append((p["round"], session_id, best, nth))
    return found[:last]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db")
    parser.add_argument("--last", type=int, default=30)
    parser.add_argument("--drill", action="append", choices=sorted(DRILLS))
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    names = args.drill or list(DRILLS)
    rounds = accepted(conn, args.last)
    wrong: dict[str, list[str]] = {name: [] for name in names}
    for number, session_id, best, nth in rounds:
        raws, state = capture.mails(conn, session_id), today(conn, session_id, capture.state(conn, session_id))
        for name in names:
            if problem := verdict(*asyncio.run(drill(raws, state, name, nth)), best):
                wrong[name].append(f"round {number} ({session_id}): {problem}")
    for name in names:
        print(f"{name:16} {len(rounds) - len(wrong[name])}/{len(rounds)} quoted within 5 % in time")
        for line in wrong[name]:
            print(f"    {line}")


if __name__ == "__main__":
    main()
