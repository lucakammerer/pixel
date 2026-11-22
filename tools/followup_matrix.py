"""Every mail that arrives later in a customer's session, read as the live service reads it.

uv run python -m tools.followup_matrix
uv run python -m tools.followup_matrix --db <snapshot> [--after-event <id>]

The cases of `tests/fixtures/thread-followups.json` (sender × phase of the session × intent) and the change mail
generated from its `generated` block: every language, unit system and attachment format, in every phase. Each mail
goes through `router.rules` in a database set up at its phase; a customer's mail then through `booking.reply_intent`,
`followup.read` and `followup.action`. Right means the expected kind, intent, action and changed fields. Offline, no
model call. `tools/benchmark_generalization.py` section 17 and `tests/test_generalization.py` run it.
"""

import argparse
import asyncio
import csv
import io
import json
import sqlite3
import sys
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from email.utils import parseaddr
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pixel.agents.rfq import booking, followup
from pixel.agents.router import agent as router
from pixel.agents.tracking import agent as tracking
from pixel.api.mailbox import RATEDESK, TRACKING, to_message
from pixel.config import settings
from pixel.contracts import Booking, BookingStatus, Message, Quote, QuoteStatus, RateOption, Rfq, Source
from pixel.core import db, events, poller
from pixel.core.context import Context
from pixel.core.outbox import _store
from pixel.crm import bookings, quotes, shipments
from tools.document_conflicts import RENDER

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "thread-followups.json"
T0 = datetime(2026, 11, 21, 23, 37, tzinfo=UTC)
LB = D("0.45359237")
# Our mails the case answers: id, purpose, recipient role.
OURS = {"rfq": (1, None, None), "quote": (2, "quote", "customer"), "booking_notice": (3, "booking", "customer"),
        "bid_request": (4, "bid_request", "airline"), "bid_pickup": (5, "bid_pickup", "trucker"),
        "booking_request": (6, "booking", "airline"), "ratedesk_request": (7, "ratedesk", "ratedesk")}  # fmt: skip
BOOKED = {"booked": BookingStatus.BOOKED, "confirmed": BookingStatus.CONFIRMED, "cancelled": BookingStatus.CANCELLED}


@dataclass
class Read:
    case_id: str
    want: dict
    got: dict
    ok: bool
    known_gap: str | None = None


class Files:
    def __init__(self, content: bytes = b""):
        self.content = content

    async def attachment(self, message_id: int, attachment_id: int) -> bytes:
        return self.content


def _address(session: dict, sender: str) -> str:
    return {"ratedesk": RATEDESK, "tracking": TRACKING}.get(sender) or session[sender]


def _setup(session: dict, phase: str, content: bytes) -> Context:
    """A session at `phase`: the request read, and from `quoted` on our quote sent, then accepted, booked."""
    conn = db.connect(":memory:")
    db.init(conn)
    ctx = Context(settings(), conn, Files(content), None, None, None)
    for role, kind in (("airline", "airline"), ("trucker", "trucker")):
        conn.execute("INSERT INTO carriers (name, kind, email, serves) VALUES (?, ?, ?, '')",
                     (session.get(f"{role}_name", role), kind, session[role]))  # fmt: skip
    customer = session["customer"]
    first = poller.ingest(ctx, to_message({"id": 1, "from": customer, "to": "pixel@hackloop.mail", "body": "",
                                           "subject": session["subject"], "created_at": T0.isoformat()}, "in"))  # fmt: skip
    conn.execute("INSERT INTO sessions (id, customer, state, received_at) VALUES (1, ?, 'open', ?)",
                 (customer, T0.isoformat()))  # fmt: skip
    for kind, (mid, purpose, role) in OURS.items():
        if purpose:
            to = _address(session, role)
            _store(ctx, first.model_copy(update={"id": mid, "direction": "out", "to": to, "in_reply_to": 1}), purpose)
    rfq = Rfq(session_id=1, message_id=1, customer=customer, received_at=T0, deadline_at=T0 + timedelta(minutes=6),
              **session["request"])  # fmt: skip
    events.emit(ctx, "rfq.extracted", "rfq", session_id=1, message_id=1, payload=rfq.model_dump(mode="json"))
    if phase == "open":
        return ctx
    q = session["quote"]
    option = RateOption(source=Source.PORTAL, carrier=session["airline_name"], origin=rfq.origin,
                        destination=rfq.destination, chargeable_kg=D(q["chargeable_kg"]), billed_kg=D(q["billed_kg"]),
                        freight=D(q["total"]), total=D(q["total"]), currency="EUR", total_eur=D(q["total"]),
                        transit_days=q["transit_days"], rate_id=q["rate_id"])  # fmt: skip
    status = {"declined": QuoteStatus.DECLINED, "quoted": QuoteStatus.SENT}.get(phase, QuoteStatus.ACCEPTED)
    quotes.save(ctx, Quote(id="Q-1", session_id=1, rfq=rfq, option=option, total=option.total, currency="EUR",
                           subject=f"Re: {session['subject']}", body="", status=status, created_at=T0,
                           sent_at=T0 + timedelta(seconds=32), sent_message_id=2))  # fmt: skip
    if phase in BOOKED:
        bookings.save(ctx, Booking(id="B-1", quote_id="Q-1", session_id=1, team_reference="PX-000001", via="portal",
                                   carrier=option.carrier, booking_reference=session["booking_reference"],
                                   status=BOOKED[phase], created_at=T0))  # fmt: skip
    return ctx


async def _read(session: dict, case: dict, content: bytes = b"") -> dict:
    ctx = _setup(session, case["phase"], content)
    try:
        raw = {"id": 50, "from": _address(session, case["sender"]), "to": "pixel@hackloop.mail",
               "subject": case.get("subject") or f"Re: {session['subject']}", "body": case["body"],
               "created_at": (T0 + timedelta(seconds=42)).isoformat()}  # fmt: skip
        if (answers := case.get("replies_to")) is not None:
            raw |= {"in_reply_to": OURS[answers][0], "references": [1, OURS[answers][0]]}
        if attachment := case.get("attachment"):
            raw["attachments"] = [
                {"id": 1, "filename": attachment["filename"], "content_type": "", "size": len(content)}
            ]
        message = poller.ingest(ctx, to_message(raw, "in"))
        kind, rule = router.rules(ctx, message)
        got = {"kind": kind.value if kind else None, "rule": rule}
        if kind is router.MailKind.CUSTOMER_REPLY:
            quote = quotes.for_session(ctx, 1)
            said = await booking.reply_intent(ctx, message)
            read = await followup.read(ctx, message, quote, await booking._request(ctx, 1, quote), said)
            got |= {"intent": read.intent, "action": followup.action(read),
                    "changes": {k: str(v) for k, v in read.changes.items()}}  # fmt: skip
        if "shipment_sessions" in case["expected"] and (
            kind is router.MailKind.TRACKING_QUESTION or got.get("action") == "track"
        ):
            matched, answer = await _track(ctx, session, message, case.get("shipment_status", "confirmed"))
            got["shipment_sessions"] = matched
            if "answer_includes" in case["expected"]:
                got["answer_includes"] = [part for part in case["expected"]["answer_includes"] if part in answer]
        return got
    finally:
        ctx.db.close()


async def _track(ctx: Context, session: dict, message: Message, status: str) -> tuple[list[int], str]:
    """Run the real tracking handler against an own shipment and one belonging to another customer."""
    quote = quotes.for_session(ctx, 1)
    assert quote is not None
    other = session["other_shipment"]
    for sid, customer, reference in (
        (1, session["customer"], session["booking_reference"]),
        (2, other["customer"], other["booking_reference"]),
    ):
        shipment = shipments.create(
            ctx,
            quote.model_copy(
                update={
                    "session_id": sid,
                    "rfq": quote.rfq.model_copy(update={"customer": customer}),
                }
            ),
        )
        shipment_status = status if sid == 1 else "confirmed"
        shipments.save(ctx, shipment.model_copy(update={"booking_reference": reference, "status": shipment_status}))
    with (
        patch.object(tracking.outbox, "send", AsyncMock()) as send,
        patch.object(tracking.compose, "localize", side_effect=lambda _ctx, _lang, _sid, body, *args: body),
    ):
        await tracking.handle(ctx, message)
    row = ctx.db.execute(
        "SELECT payload FROM events WHERE kind = 'tracking.answered' AND message_id = ?", (message.id,)
    ).fetchone()
    return json.loads(row["payload"])["shipment_sessions"], send.await_args.kwargs["body"]


def _same(want: dict, got: dict) -> bool:
    """Every expected key matches; a changed figure within 0.1% (pounds and litres convert)."""
    for key, value in want.items():
        if key != "changes":
            if got.get(key) != value:
                return False
            continue
        changes = got.get("changes", {})
        if set(changes) != set(value):
            return False
        for field, figure in value.items():
            try:
                if abs(D(changes[field]) - D(figure)) > D(figure) / 1000:
                    return False
            except ArithmeticError:
                if changes[field] != figure:
                    return False
    return True


def _document(fmt: str, cargo: dict) -> tuple[str, bytes]:
    block = [("Total pieces:", str(cargo["pieces"])), ("Total gross weight (kg):", cargo["weight_kg"]),
             ("Total volume (cbm):", cargo["volume_cbm"])]  # fmt: skip
    if fmt == "txt":
        return "packing-list.txt", "\n".join(["PACKING LIST", *(f"{k} {v}" for k, v in block)]).encode()
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Pieces", "Gross weight (kg)", "Volume (cbm)"])
        writer.writerow([cargo["pieces"], cargo["weight_kg"], cargo["volume_cbm"]])
        return "packing-list.csv", out.getvalue().encode()
    return f"packing-list.{fmt}", RENDER[fmt]("Packing List", "PL-1", block, [], [])


def generated(fixture: dict) -> list[tuple[dict, bytes]]:
    """The change mail in every language × unit system in the body, and behind every attachment format."""
    spec = fixture["generated"]
    cargo = spec["cargo"]
    kg, cbm = D(cargo["weight_kg"]), D(cargo["volume_cbm"])
    figures = {"kg": cargo["weight_kg"], "cbm": cargo["volume_cbm"], "t": f"{kg / 1000}",
               "lbs": f"{(kg / LB).quantize(D('0.1'))}", "litres": f"{(cbm * 1000).normalize():f}"}  # fmt: skip
    cases = []
    for phase, action in spec["phases"].items():
        want_changes = {
            "weight_kg": cargo["weight_kg"],
            "volume_cbm": cargo["volume_cbm"],
            "pieces": str(cargo["pieces"]),
        }
        want = {"kind": "customer_reply", "intent": "change", "action": action, "changes": want_changes}
        for lang, template in spec["change"].items():
            for units, (weight, volume) in spec["units"].items():
                body = template.format(pieces=cargo["pieces"], weight=weight.format(**figures),
                                       volume=volume.format(**figures))  # fmt: skip
                cases.append(({"id": f"{phase}/{lang}/{units}", "sender": "customer", "phase": phase,
                               "replies_to": "quote" if phase != "open" else "rfq", "body": body,
                               "expected": want}, b""))  # fmt: skip
        for lang, body in spec["attached"].items():
            for fmt in spec["formats"]:
                name, content = _document(fmt, cargo)
                cases.append(({"id": f"{phase}/{lang}/{fmt}", "sender": "customer", "phase": phase,
                               "replies_to": "quote" if phase != "open" else "rfq", "body": body,
                               "attachment": {"filename": name}, "expected": want,
                               "known_gap": spec.get("known_gaps", {}).get(fmt)}, content))  # fmt: skip
    return cases


def audit(path: Path = FIXTURE) -> list[Read]:
    fixture = json.loads(path.read_text())
    session = fixture["session"]
    rows = [(case, _content(case)) for case in fixture["cases"]] + generated(fixture)

    async def run() -> list[Read]:
        results = []
        for case, content in rows:
            got = await _read(session, case, content)
            results.append(Read(case["id"], case["expected"], got, _same(case["expected"], got), case.get("known_gap")))
        return results

    return asyncio.run(run())


def _content(case: dict) -> bytes:
    attachment = case.get("attachment")
    return "\n".join(attachment["lines"]).encode() if attachment else b""


# In event order, these transitions reconstruct the session phase before the "mail.in" event.
LIVE_PHASE = {
    "quote.sent": "quoted",
    "customer.accepted": "accepted",
    "customer.declined": "declined",
    "booking.booked": "booked",
    "booking.confirmed": "confirmed",
    "booking.cancelled": "cancelled",
}
LIVE_TRACE = {
    "route": ("kind", "rule", "by"),
    "followup.read": ("phase", "intent", "changes", "cap"),
    "human.note": ("phase", "intent"),
    "booking.confirmed": (),
    "booking.cancelled": (),
    "customer.accepted": (),
    "customer.declined": (),
    "tracking.answered": ("shipment_sessions", "statuses"),
    "bid.received": ("read_by", "ignored"),
    "error": ("where",),
}


def recorded_census(path: Path, after_event: int = 0) -> Iterator[dict]:
    """Recorded mail and scoring evidence from a SQLite snapshot. No bodies, attachments or outgoing actions."""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        mails = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, session_id, kind, purpose, direction, sender, sent_at, in_reply_to FROM messages"
            )
        }
        customers = dict(conn.execute("SELECT id, customer FROM sessions"))
        carriers = {address.lower(): kind for address, kind in conn.execute("SELECT email, kind FROM carriers")}
        phases: dict[int, list[tuple[int, str]]] = defaultdict(list)
        arrivals: dict[int, int] = {}
        trace: dict[int, list[dict]] = defaultdict(list)
        scores = {}
        scored_sessions = {}
        end = None
        for event in conn.execute("SELECT id, ts, kind, session_id, message_id, payload FROM events ORDER BY id"):
            end = event
            eid, _, kind, sid, mid, payload = event
            if kind == "mail.in":
                arrivals[mid] = eid
            if kind in LIVE_PHASE:
                phases[sid].append((eid, LIVE_PHASE[kind]))
            if kind in LIVE_TRACE and mid is not None:
                data = json.loads(payload)
                trace[mid].append(
                    {
                        "event_id": eid,
                        "event": kind,
                        "session_id": sid,
                        **{key: data[key] for key in LIVE_TRACE[kind] if key in data},
                    }
                )
            elif kind == "score":
                score = json.loads(payload)
                scores[score["round"]] = score
            elif kind == "feedback":
                scored_sessions[json.loads(payload)["round"]] = sid

    if end is None:
        return
    yield {"record": "snapshot", "last_event_id": end["id"], "last_event_at": end["ts"], "after_event": after_event}
    requests = {m["sent_at"]: m["session_id"] for m in mails.values() if m["kind"] == "customer_rfq"}
    for round_no, score in sorted(scores.items()):
        yield {
            "record": "score",
            "round": round_no,
            "session_id": scored_sessions.get(round_no) or requests.get(score["sent_at"]),
            "closed": score["raw"].get("closed"),
            "cost_pending": score.get("cost_pending"),
            **{key: score[key] for key in ("verdict", "reason", "points", "total", "booked")},
        }
    replies: dict[int, list[dict]] = defaultdict(list)
    for mail in sorted(mails.values(), key=lambda mail: mail["id"]):
        if mail["direction"] == "out" and mail["in_reply_to"] is not None:
            replies[mail["in_reply_to"]].append({"message_id": mail["id"], "purpose": mail["purpose"]})
    for mid, mail in sorted(mails.items()):
        sid, kind = mail["session_id"], mail["kind"]
        if mail["direction"] != "in" or (mid == sid and kind not in ("booking_confirmation", "tracking_question")):
            continue
        arrival = arrivals.get(mid)
        if after_event and (arrival is None or arrival <= after_event):
            continue
        # Standalone confirmations can be matched to a booking in another thread. The event gives that session.
        outcome = trace.get(mid, [])
        confirmed = next((e["session_id"] for e in outcome if e["event"] == "booking.confirmed"), None)
        phase_session = sid
        if mid == sid:
            tracked = next(
                (e.get("shipment_sessions", []) for e in reversed(outcome) if e["event"] == "tracking.answered"), []
            )
            phase_session = confirmed or (tracked[0] if len(tracked) == 1 else None)
        phase = "unknown"
        if arrival is not None and phase_session is not None:
            history = phases.get(phase_session, [])
            index = bisect_left(history, (arrival, ""))
            phase = history[index - 1][1] if index else "open"
        address = parseaddr(mail["sender"])[1].lower()
        parent = mails.get(mail["in_reply_to"], {})
        if address == TRACKING:
            role = "tracking"
        elif address == RATEDESK:
            role = "rate_desk"
        elif address in carriers:
            role = carriers[address]
        elif address == customers.get(sid, "").lower() or kind == "customer_reply":
            role = "customer"
        else:
            role = "unknown"
        yield {
            "record": "mail",
            "message_id": mid,
            "session_id": sid,
            "linked_booking_session": confirmed,
            "arrival_event_id": arrival,
            "sent_at": mail["sent_at"],
            "sender_role": role,
            "phase_at_arrival": phase,
            "kind": kind,
            "reply_purpose": parent.get("purpose"),
            "trace": outcome,
            "outgoing_replies": replies.get(mid, []),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, help="Report recorded outcomes from a read-only SQLite snapshot")
    parser.add_argument("--after-event", type=int, default=0, help="Only include mail received after this event ID")
    args = parser.parse_args()
    if args.db:
        for record in recorded_census(args.db, args.after_event):
            print(json.dumps(record))
        return 0
    if args.after_event:
        parser.error("--after-event requires --db")
    results = audit()
    wrong = [r for r in results if not r.ok and not r.known_gap]
    print(f"{sum(r.ok for r in results)}/{len(results)} later mails read and acted on right")
    for r in results:
        if not r.ok or r.known_gap:
            mark = "GAP " if r.known_gap and not r.ok else "ok  " if r.ok else "FAIL"
            print(f"  {mark} {r.case_id}: want {r.want}, got {r.got}" + (f" ({r.known_gap})" if r.known_gap else ""))
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
