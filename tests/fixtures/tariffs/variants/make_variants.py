"""Writes the changed-layout tariff sheets in this folder and `variants.jsonl`, their expected outcome.

Run from the repo root: `uv run python tests/fixtures/tariffs/variants/make_variants.py`. Each sheet is the start
kit's text layout with one change of the kind an unannounced update could bring. The expected prices are computed
here, independently of `pixel/pricing/calc.py`, from the rules printed in every sheet. The carrier is fictional; the
lane values are copied from one start-kit sheet only so the numbers are realistic.
"""

import json
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path

OUT = Path(__file__).parent
CARRIER = "Testline Cargo"
LANES = {
    "CGN-SYD": {
        "transit": 5,
        "minimum": "75.00",
        "fuel": "0.76",
        "security": "0.21",
        "rates": {0: "5.92", 45: "5.03", 100: "3.87", 250: "3.02", 500: "2.33", 1000: "1.86"},
    },
    "DUS-JFK": {
        "transit": 5,
        "minimum": "100.00",
        "fuel": "1.09",
        "security": "0.13",
        "rates": {0: "3.96", 45: "3.21", 100: "2.82", 250: "2.34", 500: "1.83", 1000: "1.50"},
    },
}
FEWER_BREAKS = {  # no 100 or 500 break, a new one at 300
    "CGN-SYD": {0: "5.92", 45: "5.03", 300: "2.90", 1000: "1.86"},
    "DUS-JFK": {0: "3.96", 45: "3.21", 300: "2.20", 1000: "1.50"},
}
SMALL_MINIMUMS = {"CGN-SYD": "25.00", "DUS-JFK": "28.00"}
SPOTS = [("CGN-SYD", "120"), ("DUS-JFK", "280")]

# (name, what changed, options for `render`, rates per lane if changed, minimums per lane if changed)
VARIANTS = [
    ("control", "the kit's text layout unchanged, as a control", {}, None, None),
    ("fewer-breaks", "different weight breaks: -45, +45, +300, +1000", {}, FEWER_BREAKS, None),
    ("usd", "currency USD instead of EUR", {"currency": "USD"}, None, None),
    ("decimal-comma", "decimal commas in every price", {"comma": True}, None, None),
    ("iso-dates-december", "next month's validity, ISO dates", {"dates": ("2026-12-01", "2026-12-31")}, None, None),
    ("transit-d-plus", "transit written as D+5", {"transit_format": "D+{}"}, None, None),
    ("minimum-before-transit", "Minimum and Transit columns swapped", {"minimum_first": True}, None, None),
    (
        "minimum-before-transit-small",
        "Minimum and Transit swapped, minimums under 30",
        {"minimum_first": True},
        None,
        SMALL_MINIMUMS,
    ),
    (
        "surcharge-columns",
        "fuel and security as table columns, no surcharge section",
        {"surcharges": "columns"},
        None,
        None,
    ),
    ("surcharge-missing", "one lane has no surcharge line", {"surcharges": "missing_one"}, None, None),
]
ONLY_REFUSAL_IS_SAFE = {"surcharge-missing"}  # a lane without surcharges cannot be priced right
GAPS: dict[str, str] = {}
# A sheet in another currency is priced with the rate the portal last quoted for it (`engine._fx_eur`).
FX_EUR = {"USD": "0.92"}


def price(lane: str, kg: str, rates: dict[int, str], minimum: str) -> tuple[D, D]:
    """Cheapest all-in total over the weight's own bracket and every higher break, and the weight billed."""
    facts, weight = LANES[lane], D(kg)
    own = max(start for start in rates if start <= weight)
    best = None
    for start in [own] + [s for s in sorted(rates) if s > weight]:
        billed = max(weight, D(start))
        freight = max(D(rates[start]) * billed, D(minimum))
        total = (freight + billed * (D(facts["fuel"]) + D(facts["security"]))).quantize(D("0.01"), ROUND_HALF_UP)
        if best is None or total < best[0]:
            best = (total, billed)
    return best


def render(
    rates: dict[str, dict[int, str]],
    minimums: dict[str, str],
    currency: str = "EUR",
    comma: bool = False,
    dates: tuple[str, str] = ("01.11.2026", "30.11.2026"),
    transit_format: str = "{}",
    minimum_first: bool = False,
    surcharges: str = "section",
) -> str:
    def num(text: str) -> str:
        return text.replace(".", ",") if comma else text

    starts = sorted(rates["CGN-SYD"])
    columns = ["Lane", "Minimum", "Transit"] if minimum_first else ["Lane", "Transit", "Minimum"]
    breaks = [f"-{starts[1]}"] + [f"+{s}" for s in starts[1:]]
    extra = ["Fuel", "Security"] if surcharges == "columns" else []
    lines = [
        f"{CARRIER} | Air export tariff",
        f"Currency    {currency}",
        f"Valid from  {dates[0]}",
        f"Valid to    {dates[1]}",
        f"All rates are per kg of chargeable weight, in {currency}.",
        "Chargeable weight is the higher of actual weight and volume weight (1 cbm = 167 kg).",
        "Fuel and security surcharges are charged per kg of the weight the freight is billed on.",
        "The minimum charge applies to the freight before surcharges.",
        "",
        "".join(c.ljust(12) for c in columns) + "".join(c.ljust(10) for c in breaks + extra),
    ]
    for lane, facts in LANES.items():
        transit = transit_format.format(facts["transit"]).rjust(10) + "  "
        minimum = f"{currency} {num(minimums[lane])}".ljust(12)
        cells = [lane.ljust(12)] + ([minimum, transit] if minimum_first else [transit, minimum])
        cells += [f"{currency} {num(rate)}".ljust(10) for _, rate in sorted(rates[lane].items())]
        if surcharges == "columns":
            cells += [f"{currency} {num(facts['fuel'])}".ljust(10), f"{currency} {num(facts['security'])}".ljust(10)]
        lines.append("".join(cells).rstrip())
    if surcharges in ("section", "missing_one"):
        lines += ["", "Surcharges per kg of billed weight"]
        for lane, facts in LANES.items():
            if surcharges == "missing_one" and lane == "DUS-JFK":
                continue
            lines.append(
                f"{lane}: fuel {currency} {num(facts['fuel'])} per kg, "
                f"security {currency} {num(facts['security'])} per kg"
            )
    return "\n".join(lines) + "\n"


def iso(day: str) -> str:
    return day if "-" in day else f"{day[6:]}-{day[3:5]}-{day[:2]}"


def main() -> None:
    rows = []
    for name, change, options, changed_rates, changed_minimums in VARIANTS:
        rates = changed_rates or {lane: facts["rates"] for lane, facts in LANES.items()}
        minimums = changed_minimums or {lane: facts["minimum"] for lane, facts in LANES.items()}
        filename = f"testline-cargo-{name}.txt"
        (OUT / filename).write_text(render(rates, minimums, **options))
        spots = []
        for lane, kg in SPOTS:
            total, billed = price(lane, kg, rates[lane], minimums[lane])
            spots.append({"lane": lane, "weight_kg": kg, "total": str(total), "billed_kg": str(billed)})
        dates = options.get("dates", ("01.11.2026", "30.11.2026"))
        row = {"known_gap": GAPS[name]} if name in GAPS else {}
        row |= {
            "file": filename,
            "change": change,
            "carrier": CARRIER,
            "currency": options.get("currency", "EUR"),
            "valid_from": iso(dates[0]),
            "valid_to": iso(dates[1]),
            "lanes": len(LANES),
            "safe_outcomes": ["refused"] if name in ONLY_REFUSAL_IS_SAFE else ["parsed right", "refused"],
            "lane_facts": {
                lane: {"transit_days": facts["transit"], "minimum": minimums[lane]} for lane, facts in LANES.items()
            },
            "spots": spots,
        }
        if currency := options.get("currency"):
            row["fx_eur"] = FX_EUR[currency]
        rows.append(row)
    (OUT / "variants.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


if __name__ == "__main__":
    main()
