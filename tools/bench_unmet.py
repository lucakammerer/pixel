"""Score the last reader's prompt (`pixel/agents/rfq/unmet.py` `read`) on stored rounds: the first round of each new
requirement we lost, with the reply we sent then, and accepted rounds of the same kinds, where nothing is unmet.

    uv run python -m tools.bench_unmet

It spends through the gateway, and that spend counts in the 18-minute window it runs in (R28): one
`PIXEL_MODEL_CHECK` call per case of `tests/fixtures/unmet-asks.json`, about 0.025 USD, which is 10 cost points
taken from the quotes accepted in that window.
"""

import asyncio
import dataclasses
import json
import tempfile
from pathlib import Path

from pixel import llm
from pixel.agents.rfq import unmet
from pixel.app import build_context
from pixel.config import settings

CASES = Path(__file__).parent.parent / "tests" / "fixtures" / "unmet-asks.json"
WAIT_S = 30


async def run() -> None:
    path = str(Path(tempfile.mkdtemp()) / "bench.sqlite3")
    ctx = build_context(dataclasses.replace(settings(), dry_run=True, db_path=path))
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    right = 0
    for case in cases:
        wanted = sorted({e["class"] for e in case["expected"]})
        try:
            asks = await unmet.read(
                ctx, case["request"], case["subject"], case["reply"], tuple(case["files"]), WAIT_S, None
            )
        except Exception as exc:  # noqa: BLE001  live this request's quote leaves unread
            print(f"FAILED {case['name']}: {type(exc).__name__}")
            continue
        found = {a["class"] for a in asks}
        ok = set(wanted) <= found if wanted else not found & set(unmet.HELD)  # a wording ask alerts nobody
        right += ok
        print(f"{'ok    ' if ok else 'WRONG '} {case['name']}: expected {wanted or 'nothing'}")
        for a in asks:
            print(f"         {a['class']}: {a['sentence'][:110]!r}")
    calls = ctx.db.execute("SELECT COUNT(*), SUM(tokens_in), SUM(tokens_out) FROM events WHERE kind = 'llm.call'")
    count, tokens_in, tokens_out = calls.fetchone()
    usd = llm.spend_usd(ctx)
    print(f"{right} of {len(cases)} right; {count} calls, {tokens_in} tokens in, {tokens_out} out, {usd:.4f} USD, "
          f"{usd / max(count, 1):.5f} USD per call ({usd / max(count, 1) * 400:.2f} cost points per quote)")  # fmt: skip


if __name__ == "__main__":
    asyncio.run(run())
