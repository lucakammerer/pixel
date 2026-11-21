# The arithmetic

How a price is computed and chosen. All of it is code in `pixel/pricing/calc.py` and `pixel/pricing/selection.py`,
Decimal only; a model never computes, compares or writes a price (P1).

## A sheet price (`calc.price_tariff`)

1. **Chargeable weight** = max(gross kg, cbm × 167) (R11), unrounded.
2. **Billed weight**: the chargeable weight, or a higher break when that makes the all-in total cheaper (R13). Every
   higher break is tried, not only the next one, and the comparison includes the surcharges, which grow with the
   billed weight.
3. **Rate**: the rate of the last break at or below the billed weight; a weight exactly on a break takes that
   break. `-45` in a sheet is the break at 0 kg.
4. **Freight** = rate × billed weight, at least the lane's minimum. The minimum applies to the freight only (R12).
5. **Fuel** and **security** = their rate per kg × billed weight.
6. **Total** = freight + fuel + security, rounded once, half up, to cents. The parts on the `RateOption` are
   rounded for display and can differ from the total by a cent; the mail shows only the total.
7. No margin (R17).

## Other sources

- **Portal** (`api/portal.py`): its `total` is already freight + fuel + security; it gives no split, so
  `freight = total`.
  `portal_rates._at_next_break` reads the airline's next weight break as well and bills that break when its total
  is lower (R13); the option then carries the break as billed weight and that read's rate id.
- **Bids** (`pricing/bids.py`): the carrier's stated all-in total; when only charges are listed (read by the model),
  code adds them up.
- **Rate desk** (`bids.parse_desk`): one option per airline the desk prices, with the total, transit days, rate id
  and validity it states.
- **Pickup** (`calc.with_pickup`): a trucker's price in EUR, added to every airline option, converted into the
  option's currency with its exchange rate when that is not EUR.
- **EUR** (`calc.to_eur`): amount × the rate's own exchange rate (EUR per unit), rounded to cents.

## Choosing (`selection.select`)

- Cheapest: lowest total in EUR; fastest: fewest transit days. Ties: cheaper, then faster, then carrier name.
- Carrier bids first pick one winner by `selection.balanced` (R15): each bid scores its share of the best price
  and its share of the best speed (best / own), equal weights, highest sum wins. Speed is transit days for an
  airline and pickup hours for a trucker. A door-to-port quote adds the balanced trucker's price, not the
  cheapest (`engine.combine`). Evidence: `tests/fixtures/pickup-bids.json` holds the trucker bids of two scored
  requests and the bid the scorer's best price contained; this formula picks both, min-max scaling and ranks
  pick the wrong one in the first. Only that winner competes with sheets, portal and the rate desk.
- The rule is not fixed: `core/feedback.py` reads our points after every request, works back to the scorer's best
  price (accuracy falls linearly to 0 at 10% off the best price, in either direction: fitted on scored requests, not
  stated by 5U) and finds the option that total belongs to. `pickup_rule` then uses the entry of `selection.RULES`
  that picks the scorer's trucker most often, once it leads the default by `feedback.RULE_LEAD` requests: two
  requests that lost points on the rule correct the next one, a single misread round does not.

## Assumptions until 5U answers (plan §0)

Each lives in one place; change it there.

| Q | Assumption | Where |
| --- | --- | --- |
| Q4 | The best price is the all-in total of the option the customer asked for, pickup included | `engine.combine` |
| Q6 | Balanced = share of the best price plus share of the best speed, equal weights, among bids only (fits two scored requests; the exact formula is unconfirmed); the winner then competes by the customer's wish | `selection.balanced`, `selection.select` |
| Q7 | We quote in the chosen rate's own currency and compare in EUR with the rate's exchange rate | `selection.select`, `compose.render` |
| Q13 | The pickup zone is the first two digits of the postcode | `pickup.zone_of` |
