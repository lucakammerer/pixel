"""The night shift links a run of lost rounds to the change that names it and went live after its last loss."""

from datetime import UTC, datetime, timedelta

from pixel.ui import nightshift

T = datetime(2026, 11, 21, 22, 0, tzinfo=UTC)
LOG = """11-21 22:10:00 deployed 0000000..aaaaaaa, restarted once
11-21 22:20:00 bbbbbbb green, waiting for the window (urgent)
11-21 22:20:05 deployed aaaaaaa..bbbbbbb, restarted once
"""


def test_the_fix_is_the_named_change_live_after_the_last_loss():
    found = [{"round": n, "at": T + timedelta(minutes=6 * (n - 76)), "session": n, "lost": n < 80,
              "reason": "no_pdf_offer" if n < 80 else None, "points": "60" if n == 80 else None}
             for n in range(76, 81)]  # fmt: skip
    commits = [
        {"sha": "a" * 40, "at": T + timedelta(minutes=5), "pr": "243", "title": "", "text": "round 76: no PDF"},
        {"sha": "c" * 40, "at": T + timedelta(minutes=9), "pr": "244", "title": "", "text": "too_low wording"},
        {"sha": "b" * 40, "at": T + timedelta(minutes=15), "pr": "249", "title": "", "text": "round 78: %%EOF"},
    ]
    moves, green = nightshift.deploy_log(LOG, T)
    nightshift.live_at(commits, moves)
    (item,) = nightshift.incidents(found, commits, [(T + timedelta(minutes=1), "error")])
    assert item["fix"]["pr"] == "249" and [c["pr"] for c in item["tried"]] == ["243"]  # #244: another reason
    assert item["back"]["round"] == 80 and item["repair"] == timedelta(minutes=20, seconds=5)
    assert green["bbbbbbb"][1] and item["signals"] == ["error"]
