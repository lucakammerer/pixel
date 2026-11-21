import pytest

from tools import board_me

RUNS = """# Runs

| Round | Sent | Answered | Seconds | Verdict | Reason | Accuracy | Cost | Time | Bonus | Total | Booked | Commit | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Changes we noticed
"""


pytest_plugins = ["test_crm"]  # the `pipe` fixture


class Board:
    def __init__(self, rows):
        self.rows = rows

    async def me(self):
        return {"team": "pixel", "requests": self.rows}


@pytest.fixture
def runs(tmp_path):
    path = tmp_path / "RUNS.md"
    path.write_text(RUNS)
    return path


def row(n, verdict="accepted", cost=None):
    return {"round": n, "sent_at": "2026-11-21T16:00:00Z", "verdict": verdict, "reason": None,
            "points": {"accuracy": 45, "cost": cost, "time": 15}, "total": 60 + (cost or 0), "booked": False}  # fmt: skip


async def test_twice_adds_nothing_twice(pipe, runs):
    pipe.ctx.board = Board([row(1), row(2, "rejected")])
    assert await board_me.run(pipe.ctx, runs) == 2
    assert await board_me.run(pipe.ctx, runs) == 0
    text = runs.read_text()
    assert text.count("| 2 |") == 1 and text.index("| 2 |") < text.index("| 1 |") and "## Changes we noticed" in text


async def test_late_cost_points_update_the_row(pipe, runs):
    pipe.ctx.board = Board([row(1)])
    await board_me.run(pipe.ctx, runs)
    runs.write_text(runs.read_text().replace(" |\n\n## Changes", " checked by hand |\n\n## Changes"))
    pipe.ctx.board = Board([row(1, cost=38)])
    assert await board_me.run(pipe.ctx, runs) == 1
    (line,) = [x for x in runs.read_text().splitlines() if x.startswith("| 1 |")]
    assert "| 38 |" in line and "checked by hand" in line
    payloads = pipe.ctx.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'score'").fetchone()[0]
    assert payloads == 2
