"""The scorer's checks (bench/NEXT_CASES.md, axis R) on the first reply to each predicted case, offline.

uv run python -m tools.checklist                     # every case of tests/fixtures/next-cases.json
uv run python -m tools.checklist currency-usd -v     # one case, with the reply

Each case is replayed as `tools.kpi` does it, on the start kit with no model. The first mail to the customer that
holds a price or declines is the reply the scorer reads (a question is not scored: rounds 103, 131, 132). It is held
against the case's `expected`: a price at all, the currency, one total per option, words it must or must not hold,
the greeting, the language. `body_states` is prose and stays for a person (`-v` prints the reply). A case the
rules give up on offline is `model`: live the model reads it, offline nothing does.
"""

import argparse
import asyncio
import json
from pathlib import Path

from pixel.agents.rfq import check, compose
from tools import kpi, replay

ROOT = Path(__file__).parent.parent
CASES = ROOT / "tests" / "fixtures" / "next-cases.json"
SCORED = ("quote", "fallback_quote", "no_price")


def reply(evs: list) -> dict | None:
    out = [e.payload for e in evs if e.kind == "mail.out" and e.payload.get("purpose") in SCORED]
    return out[0] if out else None


def judge(expected: dict, sent: dict | None) -> list[str]:
    """The checks of the rubric this case's expectation names that the reply fails."""
    if sent is None:
        return ["no answer"]
    body, failed = sent["body"], []
    priced = sent["purpose"] != "no_price"
    if not priced:  # every case here is in scope
        failed.append("declined_in_scope")
    if (currency := expected.get("currency")) and currency not in body:
        failed.append(f"currency {currency} not stated")
    if (options := expected.get("options")) and len(check.amounts(check.ours(body))) < len(options):
        failed.append(f"one_of_{len(options)}: {len(check.amounts(check.ours(body)))} amounts")
    failed += [f"missing '{w}'" for w in expected.get("body_contains", []) if w not in body]
    failed += [f"holds '{w}'" for w in expected.get("body_lacks", []) if w.lower() in body.lower()]
    if (greeting := expected.get("greeting")) and greeting not in body.split("\n", 1)[0]:
        failed.append(f"greeting is not {greeting}")
    if lang := expected.get("language"):
        counts = compose._words_lang(check.ours(body))
        if max(counts, key=counts.get) != lang:
            failed.append(f"wrong_language: reads {max(counts, key=counts.get)}")
    return failed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", help="case names; default: all")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each reply")
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    kit = await kpi._kit()
    wrong = 0
    for case in cases:
        path = ROOT / "tests" / "fixtures" / case["fixture"]
        if args.cases and path.stem not in args.cases or "infra" in path.parts:
            continue
        evs = await replay.run(replay.raws_of(json.loads(path.read_text(encoding="utf-8"))), False, kit, as_live=True)
        sent = reply(evs)
        failed = judge(case["expected"], sent)
        model = (sent is None or sent["purpose"] == "no_price") and any(e.kind == "llm.call" for e in evs)
        verdict = "model" if model else "wrong" if failed else "right"
        gap = " (known gap)" if case.get("known_gap") else ""
        wrong += verdict == "wrong" and not gap
        print(f"{path.stem:40} {verdict:5}{gap} {'; '.join(failed)}")
        if args.verbose and sent:
            print("    " + sent["body"].replace("\n", "\n    "))
            if states := case["expected"].get("body_states"):
                print(f"    -> by hand: {states}")
    print(f"wrong outside the known gaps: {wrong}")
    raise SystemExit(1 if wrong else 0)


if __name__ == "__main__":
    asyncio.run(main())
