"""Pull GET /v1/board/me, store each row as a `score` event and append new rows to bench/RUNS.md.

uv run python -m tools.board_me
"""

import asyncio
import json
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pixel.core import events
from pixel.core.context import Context

RUNS = Path(__file__).parent.parent / "bench" / "RUNS.md"
COLUMNS = ("round", "sent_at", "answered_at", "seconds", "verdict", "reason", "teams", "factor", "accuracy", "cost",
           "time", "bonus", "total", "booked", "commit", "note")  # fmt: skip
KINDS = ("accuracy", "time", "cost", "humanity", "booking", "tracking", "adjustment")  # as the board lists them
HIDDEN = ("accuracy", "time")  # left out of each row until the scores are revealed
RARE = "Rare rounds (one to three teams accepted by the scorer) we were accepted in:"
BONUS = ("humanity", "booking", "tracking", "adjustment")  # the RUNS.md column "Bonus"


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def rows_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-request rows, wherever the response keeps them."""
    for key in ("requests", "rounds", "results", "rows", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return next(
        (v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict) and "verdict" in v[0]), []
    )


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    """One /v1/board/me row in our shape. `points` holds every kind the scorer reports for the row (`KINDS`); the
    board leaves accuracy and time out until the scores are revealed, and cost out while `cost_pending`."""
    nested = row.get("points") if isinstance(row.get("points"), dict) else {}
    points = {}
    for k in KINDS:
        value = _pick(nested, k, f"{k}_points")
        if value is None:
            value = _pick(row, f"{k}_points")
        if value is not None:
            points[k] = value
    seconds = _pick(row, "seconds", "response_seconds", "response_s")
    if seconds is None and row.get("sent_at") and row.get("answered_at"):
        try:
            seconds = round(
                (datetime.fromisoformat(row["answered_at"]) - datetime.fromisoformat(row["sent_at"])).total_seconds(), 2
            )
        except (TypeError, ValueError):
            pass
    return {
        "round": _pick(row, "round_no", "round", "round_id", "request_id", "id"),
        "sent_at": _pick(row, "sent_at", "sent", "requested_at"),
        "answered_at": _pick(row, "answered_at", "answered", "replied_at"),
        "seconds": seconds,
        "verdict": _pick(row, "verdict", "status"),
        "reason": _pick(row, "reason", "reason_code"),
        "points": points,
        "total": _pick(row, "total", "points_total", "request_total"),
        "booked": _pick(row, "booked"),
        "cost_pending": bool(row.get("cost_pending")),
        "raw": row,
    }


def rounds_of(public: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per closed round of the public board: `teams` the scorer accepted, the board's points `factor` and `rare`,
    whether so few teams were accepted that `few_winners` multiplies the round (one to as many teams as it lists
    factors)."""
    factors = len(((public.get("rules") or {}).get("few_winners") or {}).get("factors") or [])
    return {
        str(r["round_no"]): {"teams": r.get("accepted"), "factor": r.get("factor"),
                             "rare": 1 <= (r.get("accepted") or 0) <= factors}
        for r in public.get("rounds") or []
        if r.get("status") == "closed" and not r.get("voided")
    }  # fmt: skip


def rare(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Rounds the scorer accepted one to three teams in: how many, how many of them it accepted our quote in, and
    the points those rounds gave us (as the board reports them, its factor included)."""
    rows = [s for s in scores if s.get("rare")]
    won = [s for s in rows if s.get("verdict") == "accepted"]
    return {"rounds": len(rows), "won": len(won),
            "points": sum((Decimal(str(s["total"])) for s in won if s.get("total") is not None), Decimal(0))}  # fmt: skip


def _stored(ctx: Context) -> dict[str, dict[str, Any]]:
    rows = ctx.db.execute("SELECT payload FROM events WHERE kind = 'score' ORDER BY id").fetchall()
    return {str(p["round"]): p for p in (json.loads(r["payload"]) for r in rows)}


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], check=False, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return ""


def _cells(score: dict[str, Any], commit: str, note: str) -> dict[str, str]:
    cells = {k: "" if score.get(k) is None else str(score[k]) for k in COLUMNS}
    points = score.get("points") or {}
    cells.update({k: str(points.get(k, "")) for k in ("accuracy", "cost", "time")})
    bonus = [Decimal(str(points[k])) for k in BONUS if points.get(k) is not None]
    cells["bonus"] = str(sum(bonus, Decimal(0))) if bonus else ""
    cells["booked"] = {True: "yes", False: "no"}.get(score.get("booked"), cells["booked"])
    cells.update(commit=commit, note=note)
    return {k: v.replace("|", "/").replace("\n", " ") for k, v in cells.items()}


def write_runs(scores: list[dict[str, Any]], path: Path = RUNS) -> int:
    """Upserts one table row per round, newest first, keeping each row's commit and note. Returns new rows."""
    text = path.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\n## ")
    lines = head.rstrip("\n").split("\n")
    old_rare = next((i for i, line in enumerate(lines) if line.startswith(RARE)), None)
    if old_rare is not None:
        del lines[old_rare : old_rare + 2]
    summary = rare(scores)
    table = next(i for i, line in enumerate(lines) if line.startswith("| Round"))
    lines[table:table] = [f"{RARE} {summary['won']} / {summary['rounds']}, {summary['points']} points.", ""]
    start = table + 4
    existing = {}
    for line in lines[start:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == len(COLUMNS):
            existing[cells[0]] = dict(zip(COLUMNS, cells, strict=True))
    commit, added = _commit(), 0
    for score in scores:
        key = str(score["round"])
        old = existing.get(key)
        added += old is None
        existing[key] = _cells(score, old["commit"] if old else commit, old["note"] if old else "")

    def order(key: str):
        return (0, int(key)) if key.isdigit() else (1, key)

    rows = [
        "| " + " | ".join(existing[k][c] for c in COLUMNS) + " |" for k in sorted(existing, key=order, reverse=True)
    ]
    path.write_text("\n".join(lines[:start] + rows) + "\n" + (f"\n## {rest}" if sep else ""), encoding="utf-8")
    return added


async def run(ctx: Context, path: Path = RUNS) -> int:
    """Stores a `score` event for every new or changed row; returns how many it stored."""
    stored, changed = _stored(ctx), 0
    try:
        info = rounds_of(await ctx.board.public())
    except Exception:  # noqa: BLE001  the rare-round columns stay empty, the rows still land
        info = {}
    scores = [normalize(r) for r in rows_of(await ctx.board.me())]
    scores = [s | info.get(str(s["round"]), {}) for s in scores]
    for score in scores:
        if score["round"] is None:
            continue
        old = stored.get(str(score["round"]))
        if old and {k: v for k, v in old.items() if k != "raw"} == json.loads(
            json.dumps({k: v for k, v in score.items() if k != "raw"}, default=str)
        ):
            continue
        events.emit(ctx, "score", "board", payload=json.loads(json.dumps(score, default=str)))
        changed += 1
    write_runs([s for s in scores if s["round"] is not None], path)
    return changed


def main() -> None:
    from pixel.app import build_context

    print(f"{asyncio.run(run(build_context()))} new or changed rows")


if __name__ == "__main__":
    main()
