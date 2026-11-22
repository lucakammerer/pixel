"""Inventory recorded procurement inputs on an offline SQLite copy.

uv run python -m tools.supply_census <copy> > <private-scratch>/supply.jsonl
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pixel.pricing.calc import money

SUPPLY_KINDS = {"carrier_bid", "ratedesk_reply", "tariff", "booking_confirmation"}
MAIL_EVENTS = {
    "route", "bid.received", "bid.crosscheck", "booking.confirmed", "tracking.answered",
    "carrier.availability", "carrier.lanes", "tariff.ingested", "tariff.failed", "option.found",
    "option.failed", "option.vetoed", "error",
}  # fmt: skip


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _lane(option: dict) -> tuple:
    return option.get("origin"), option.get("destination")


def _tariff_key(option: dict) -> tuple:
    return option.get("carrier"), *_lane(option)


def _pickup_match(quote: dict, offer: dict) -> bool:
    if not quote.get("trucker") or quote["trucker"] != offer.get("trucker") or _lane(quote) != _lane(offer):
        return False
    if quote["currency"] != "EUR" and not quote.get("fx_eur"):
        return False
    euros = Decimal(offer["total_eur"])
    fx = Decimal(1) if quote["currency"] == "EUR" else Decimal(quote["fx_eur"])
    return money(euros / fx) == Decimal(quote["pickup"])


def _mail_trace(event: dict) -> dict:
    payload = event["payload"]
    trace = {key: event[key] for key in ("id", "kind", "ts")}
    for key in ("kind", "by", "rule", "where", "source", "version", "lanes", "carrier", "pickup", "priced",
                "read_by", "request_id", "origin", "destination", "ignored", "error"):  # fmt: skip
        if key in payload:
            trace["route_kind" if key == "kind" else key] = payload[key]
    if event["kind"] == "tariff.failed":
        trace["findings"] = payload.get("findings", [])
    if "option" in payload:
        trace["option"] = payload["option"]
    return trace


def _seconds(start: str, end: str | None) -> float | None:
    return round((_time(end) - _time(start)).total_seconds(), 3) if end else None


def _newer_bids(selected: list[int], request_for_mail: dict, mails: dict, sent_at: str) -> list[dict]:
    found = []
    for mid in selected:
        for other in request_for_mail.get(mid, []):
            if _time(mails[mid]["sent_at"]) < _time(mails[other]["sent_at"]) <= _time(sent_at):
                found.append({"quoted_message_id": mid, "later_message_id": other})
    return found


def _scored_rounds(db: sqlite3.Connection, score: dict, feedback: dict, quotes: dict):
    by_receipt = defaultdict(list)
    for session_id, received_at in db.execute("SELECT id, received_at FROM sessions"):
        by_receipt[_time(received_at)].append(session_id)
    for number, result in sorted(score.items()):
        session_id = feedback.get(number)
        link = "feedback" if session_id is not None else None
        if session_id is None and (received_at := result.get("sent_at")):
            candidates = by_receipt.get(_time(received_at), [])
            if len(candidates) == 1:
                session_id, link = candidates[0], "exact_receipt"
        sent = quotes.get(session_id, [])
        answered_at = result.get("answered_at")
        matches = [q["quote_id"] for q in sent if answered_at and _time(q["sent_at"]) == _time(answered_at)]
        yield {
            "record": "scored_round", "round": number, "verdict": result.get("verdict"),
            "reason": result.get("reason"), "session_id": session_id, "session_link": link,
            "quote_ids": [q["quote_id"] for q in sent], "scored_quote_id": matches[0] if len(matches) == 1 else None,
        }  # fmt: skip


def records(db: sqlite3.Connection):
    db.row_factory = sqlite3.Row
    by_mail = defaultdict(list)
    by_session = defaultdict(list)
    by_source = defaultdict(list)
    score, feedback = {}, {}
    portal, exceptions = [], []
    latest_event = None
    for row in db.execute("SELECT id, ts, session_id, message_id, kind, payload FROM events ORDER BY id"):
        latest_event = row
        event = dict(row)
        event["payload"] = payload = json.loads(row["payload"])
        kind, message_id = row["kind"], row["message_id"]
        option = payload.get("option", {})
        if kind.startswith("tariff."):
            source = payload.get("source") or ""
            by_source[source].append(event)
            if source.startswith("mail:"):
                message_id = int(source.split(":", 2)[1])
        if message_id is not None and kind in MAIL_EVENTS:
            by_mail[message_id].append(_mail_trace(event))
        if kind == "option.found":
            by_session[row["session_id"]].append(event)
        if kind.startswith("rate_card.") or (
            kind in {"option.found", "option.failed"} and (option or payload).get("source") == "portal"
        ):
            portal.append(event)
        if kind in {"tariff.failed", "option.failed", "option.vetoed"}:
            exceptions.append(_mail_trace(event) | {"message_id": message_id, "session_id": row["session_id"]})
        if kind == "score":
            score[payload["round"]] = payload
        if kind == "feedback" and row["session_id"] is not None:
            feedback[payload["round"]] = row["session_id"]

    if latest_event:
        yield {"record": "snapshot", "last_event_id": latest_event["id"], "last_event_at": latest_event["ts"]}

    mails = {
        row["id"]: dict(row)
        for row in db.execute(
            "SELECT id, kind, session_id, sent_at, in_reply_to, lower(sender) AS sender FROM messages WHERE direction='in'"
        )
    }
    carrier_kinds = dict(db.execute("SELECT lower(email), kind FROM carriers"))
    by_request = defaultdict(set)
    for mid, mail in mails.items():
        for received in by_mail[mid]:
            if received["kind"] == "bid.received" and received.get("request_id"):
                by_request[(received["request_id"], mail["sender"])].add(mid)
    request_for_mail = {mid: sorted(group) for group in by_request.values() for mid in group}
    tariffs = defaultdict(list)
    for db_row in db.execute("SELECT * FROM tariffs ORDER BY ingested_at, carrier, origin, destination, version"):
        tariff = dict(db_row)
        tariff["breaks"] = json.loads(tariff["breaks"])
        tariffs[_tariff_key(tariff)].append(tariff)

    used_mail, used_portal, used_sheet = defaultdict(set), defaultdict(set), defaultdict(set)
    quotes = defaultdict(list)
    for row in db.execute(
        "SELECT id, session_id, sent_at, data FROM quotes WHERE sent_at IS NOT NULL ORDER BY sent_at"
    ):
        data = json.loads(row["data"])
        option, rfq = data["option"], data["rfq"]
        quote_id, session_id, sent_at = row["id"], row["session_id"], row["sent_at"]
        before = [e for e in by_session[session_id] if _time(e["ts"]) <= _time(sent_at)]
        air_bid = option.get("evidence_message_id")
        if air_bid is not None:
            used_mail[air_bid].add(quote_id)
        rate_id = option.get("rate_id")
        for event in before:
            if rate_id and event["payload"]["option"].get("rate_id") == rate_id:
                used_portal[event["id"]].add(quote_id)
                if event["message_id"] is not None:
                    used_mail[event["message_id"]].add(quote_id)
                    if air_bid is None:
                        air_bid = event["message_id"]
        pickups = sorted(
            {
                e["message_id"]
                for e in before
                if e["message_id"] is not None and _pickup_match(option, e["payload"]["option"])
            }
        )
        bid_inputs = [air_bid] if air_bid is not None else []
        if len(pickups) == 1:
            bid_inputs.extend(pickups)
        ready_day = max(_time(rfq["received_at"]).date().isoformat(), rfq.get("ready_on") or "")
        sheet_sources, newer_sheets = [], []
        if option["source"] == "sheet":
            rows = tariffs[_tariff_key(option)]
            version = option.get("dataset_version")
            sheet_sources = sorted({t["source"] for t in rows if t["version"] == version})
            used_sheet[(*_tariff_key(option), version)].add(quote_id)
            if len(sheet_sources) == 1 and sheet_sources[0].startswith("mail:"):
                used_mail[int(sheet_sources[0].split(":", 2)[1])].add(quote_id)
            picked = [t for t in rows if t["version"] == version]
            if len(picked) == 1:
                for t in rows:
                    if t["version"] == version or not t["valid_from"] <= ready_day <= t["valid_to"]:
                        continue
                    source = t["source"]
                    source_mail = mails.get(int(source.split(":", 2)[1])) if source.startswith("mail:") else None
                    received = source_mail["sent_at"] if source_mail else None
                    newer = (t["valid_from"], _time(t["ingested_at"])) > (
                        picked[0]["valid_from"],
                        _time(picked[0]["ingested_at"]),
                    )
                    if newer and received and _time(received) <= _time(sent_at):
                        newer_sheets.append(
                            {
                                "source": source,
                                "version": t["version"],
                                "received_at": received,
                                "ingested_before_quote": any(
                                    e["kind"] == "tariff.ingested"
                                    and e["payload"]["version"] == t["version"]
                                    and _time(e["ts"]) <= _time(sent_at)
                                    for e in by_source[source]
                                ),
                            }
                        )
        expiry = option.get("valid_until")
        quotes[session_id].append(
            {
                "quote_id": quote_id,
                "sent_at": sent_at,
                "ready_on": ready_day,
                "option": option,
                "expired_at_send": _time(expiry) < _time(sent_at) if expiry else None,
                "air_bid_message_id": air_bid,
                "pickup_bid_matches": pickups,
                "sheet_sources": sheet_sources,
                "newer_sheet_candidates": newer_sheets,
                "later_bid_candidates": _newer_bids(bid_inputs, request_for_mail, mails, sent_at),
            }
        )

    pickup_matches = defaultdict(set)
    for session_id, sent in quotes.items():
        for quote in sent:
            yield {"record": "quote", "session_id": session_id, **quote}
            for message_id in quote["pickup_bid_matches"]:
                pickup_matches[message_id].add(quote["quote_id"])
    yield from _scored_rounds(db, score, feedback, quotes)

    for mid, mail in mails.items():
        carrier_kind = carrier_kinds.get(mail["sender"])
        traces = by_mail[mid]
        if mail["kind"] not in SUPPLY_KINDS and not carrier_kind and mail["kind"] not in {"tracking_question", "other"}:
            continue
        usable = next((e["ts"] for e in traces if e["kind"] in {"option.found", "tariff.ingested"}), None)
        if mail["kind"] == "booking_confirmation":
            usable = next((e["ts"] for e in traces if e["kind"] == "booking.confirmed"), None)
        yield {
            "record": "mail",
            "message_id": mid,
            "session_id": mail["session_id"],
            "route": mail["kind"],
            "carrier_kind": carrier_kind,
            "received_at": mail["sent_at"],
            "in_reply_to": mail["in_reply_to"],
            "usable_seconds": _seconds(mail["sent_at"], usable),
            "events": traces,
            "used_in_quotes": sorted(used_mail[mid]),
            "pickup_quote_matches": sorted(pickup_matches[mid]),
        }

    for versions in tariffs.values():
        for tariff in versions:
            key = (*_tariff_key(tariff), tariff["version"])
            yield {"record": "tariff_lane", **tariff, "used_in_quotes": sorted(used_sheet[key])}
    for event in portal:
        yield {"record": "portal", **event, "used_in_quotes": sorted(used_portal[event["id"]])}
    for event in exceptions:
        yield {"record": "exception", **event}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("copy", type=Path, help="local SQLite database snapshot")
    args = parser.parse_args()
    with closing(sqlite3.connect(f"{args.copy.resolve().as_uri()}?mode=ro", uri=True)) as db:
        for record in records(db):
            print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
