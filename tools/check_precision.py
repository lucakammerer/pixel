"""How often each rule of the pre-send check was right, as a markdown table.

uv run python -m tools.check_precision <database>

Per rule and level of `agents/rfq/check.py`: how often it fired (`quote.checked` findings), the rounds the scorer
accepted and the rounds it did not, by the scorer's reason. A finding counts for the round of its session
(`feedback`), with that round's newest verdict (`score`). A round marked * had the finding only on a quote that was
not the one scored: a draft that did not leave, or a requote. Read-only.
"""

import json
import sqlite3
import sys
from collections import defaultdict


def table(conn: sqlite3.Connection) -> list[str]:
    def payloads(kinds: str) -> list[tuple[int | None, dict]]:
        rows = conn.execute(f"SELECT session_id, payload FROM events WHERE kind IN ({kinds}) ORDER BY id")
        return [(session, json.loads(payload)) for session, payload in rows]

    rounds = {session: p["round"] for session, p in payloads("'feedback'") if session}
    verdicts = {p["round"]: p.get("reason") or p["verdict"] for _, p in payloads("'feedback', 'score'")}
    scored: dict[int | None, str] = {}  # only a session's first quote is scored (R4)
    for session, p in payloads("'quote.sent'"):
        scored.setdefault(session, p["quote"])
    fired: dict[tuple[str, str], int] = defaultdict(int)
    hit: dict[tuple[str, str], dict[int | None, bool]] = defaultdict(dict)  # round: on the scored quote
    for session, p in payloads("'quote.checked'"):
        for f in p["findings"]:
            key = (f["level"], f["rule"])
            fired[key] += 1
            round_no = rounds.get(session)
            hit[key][round_no] = hit[key].get(round_no, False) or p["quote"] == scored.get(session)
    lines = ["| Rule | Level | Fired | Accepted rounds | Not accepted, by the scorer's reason |", "| --- | --- | --- | --- | --- |"]  # fmt: skip
    for level, rule in sorted(fired):
        by_verdict: dict[str, list[str]] = defaultdict(list)
        for round_no, on_scored in sorted(hit[level, rule].items(), key=lambda item: item[0] or 0):
            by_verdict[verdicts.get(round_no, "unscored")].append(f"{round_no}{'' if on_scored else '*'}")
        accepted = by_verdict.pop("accepted", [])
        rest = "; ".join(f"{reason}: {', '.join(found)}" for reason, found in sorted(by_verdict.items()))
        lines.append(f"| `{rule}` | {level} | {fired[level, rule]} | {', '.join(accepted) or '—'} | {rest or '—'} |")
    return lines


if __name__ == "__main__":
    print("\n".join(table(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True))))
