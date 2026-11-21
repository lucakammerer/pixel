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
  code adds them up. A flat price "per shipment" or "per AWB" is that total; transit counts calendar, working or
  business days alike, and a range ("2-3 days") its first figure.
- **Rate desk** (`bids.parse_desk`): one option per airline the desk prices, with the total, transit days, rate id
  and validity it states.
- **Pickup** (`calc.with_pickup`): a trucker's price in EUR, added to every airline option, converted into the
  option's currency with its exchange rate when that is not EUR. Truckers price by zone and quote only with a
  five-digit postcode: a pickup known by its town alone is asked with the zone's first one (`bids._postcode`; two
  postcodes of one zone got the same price and pickup time).
- **EUR** (`calc.to_eur`): amount × the rate's own exchange rate (EUR per unit), rounded to cents.

## Choosing (`selection.select`)

- Cheapest: lowest total in EUR; fastest: fewest transit days. Ties: cheaper, then faster, then carrier name.
- Carrier bids first pick one winner by `selection.balanced` (R15): each bid's price and speed are taken as
  multiples of the best price and the best speed among the bids (own / best), equal weights, lowest sum wins. Speed
  is transit days for an airline and pickup hours for a trucker. A door-to-port quote adds the balanced trucker's
  price, not the cheapest, whatever the customer asked for (`engine.combine`). Evidence: the table below;
  `tests/fixtures/pickup-bids.json` holds the trucker bids of those rounds and the bid the scorer's best price
  contained. Best / own summed, own / worst summed and min-max scaling each miss at least one. Only the winner
  competes with sheets, portal and the rate desk.
- The rule is not fixed: `core/feedback.py` reads our points after every request, works back to the scorer's best
  price (accuracy falls linearly to 0 at 10% off the best price, in either direction: fitted on scored requests, not
  stated by 5U) and finds the option that total belongs to. `pickup_rule` then uses the entry of `selection.RULES`
  that picks the scorer's trucker most often, once it leads the default by `feedback.RULE_LEAD` requests: two
  requests that lost points on the rule correct the next one, a single misread round does not.

## Which trucker the scorer takes

Rounds of 21.11.2026 from the live host. A rule fits a round when its total, taken as the scorer's best price, gives
the points we got (accuracy falls linearly to 0 at 10% off the best price) or, for a rejection, lies more than 5%
below what we sent. The "rule's total" is the airline the scorer took plus that rule's trucker, in EUR.

| Round | Wish | Pickup place | Airline | Trucker bids (EUR, hours) | We sent | Verdict | Cheapest trucker | By wish | Balanced (`times_the_best`) | Balanced on door-to-airport total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | fastest | named, 61018 | Solace 2 d, scorer's 5100.00 (derived) | Spessart 299.03/25, Westfalenroute 266.03/23, Hansekogge 245.02/23, Nordlicht 152.01/30, Bodenseefracht 119.01/30 | 5357.76, Bodenseefracht | accepted, 48.04 | 5219.01 fits | Hansekogge 5345.02, no | Bodenseefracht 5219.01 fits | Hansekogge 5345.02, no |
| 4 | cheapest | named, 51896 | Xenon 1483.43 | Elbkante 115.98/29, Westfalenroute 187.97/11, Maintal 192.97/13, Bodenseefracht 226.96/12 | 1599.41, Elbkante | accepted, 40.62 | 1599.41 would score 60, no | Elbkante, no | Westfalenroute 1671.40 fits to the cent | Westfalenroute fits |
| 11 | cheapest | named, 21663 | Juniper 1966.50 | Odenwald 119.30/26, Spessart 120.30/28, Domblick 166.50/26, Waldrand 193.60/18, Maintal 220.60/14 | 2187.10, Maintal | accepted, 60 | 2085.80, 4.9% off, no | Odenwald, no | Maintal 2187.10 fits | Maintal fits |
| 13 | cheapest | named, 24983 | Borealis 2752.30 | Neckarstrom 348.85/11, Maintal 190.33/26, Alpenblick 122.76/30, Hansekogge 201.52/19, Flinkwagen 273.28/19 | 2875.06, Alpenblick | accepted, 48 | 2875.06 would score 60, no | Alpenblick, no | Hansekogge 2953.82 fits to the cent | Neckarstrom 3101.15, 7.3% off, no |
| 14 | cheapest | named, 42401 | Gannet 3003.86 | Rheinufer 379.38/7, Spessart 113.05/32, Westfalenroute 321.74/4, Schwabenbote 379.79/6 | 3325.60, Westfalenroute | accepted, 60 | 3116.91, 6.7% off, no | Spessart, no | Westfalenroute 3325.60 fits | Westfalenroute fits |
| 19 | fastest | "our warehouse", 51896 from round 4 | Lumen 1 d 5819.38 | Elbkante 214.06/29, Westfalenroute 356.10/11, Bodenseefracht 451.14/12, Maintal 347.09/13 | 6175.48, Westfalenroute | accepted, 60 | 6033.44, 2.4% off, no | Westfalenroute fits | Westfalenroute 6175.48 fits | Westfalenroute fits |
| 16 | fastest | "our warehouse", tainted | Helix 2 d 1490.98 | Domblick 175.10/8, Vogelsberg 86.06/29, Bodenseefracht 197.12/6 (at 36627, guessed) | 1688.10, Bodenseefracht | rejected, too high | — | — | — | — |
| 18 | cheapest | "our warehouse", tainted: the customer later wrote 51115, CGN | Juniper 2380.00 (HAM, wrong airport) | Waldrand 195.88/18, Odenwald 120.44/26, Domblick 168.40/26, Spessart 121.44/28, Maintal 222.88/14 (at 21663, guessed) | 2602.88, Maintal | rejected, too high | — | — | — | — |

- Only balanced as multiples of the best price and the best pickup time fits every round whose place the scorer
  priced as we did: 2, 4, 11, 13, 14 and 19. Round 19's place was guessed from round 4, but its full score to the
  cent shows the scorer priced the same zone. Round 2 rests on an air price derived from the points; without it the
  other five decide alone.
- The customer's wish does not choose the trucker: in rounds 11 and 14 a cheapest request scored 60 with the
  dearest or second dearest, fastest trucker. Round 18 got the dearest trucker of five for the same reason, by the
  same rule, at the same place as round 11; it lost on the airport, not on the trucker.
- Rounds 15 to 18 had no pickup place in the mail (guessed from an earlier request, or rescued by hand without a
  pickup) and say nothing about the trucker.
- `feedback.pickup_rule` on the host counts rounds 11, 13, 14 and 19: `times_the_best` 4, every other rule 3, so it
  keeps the default; rejected rounds name no trucker and cannot move it.

## Assumptions until 5U answers (plan §0)

Each lives in one place; change it there.

| Q | Assumption | Where |
| --- | --- | --- |
| Q4 | The best price is the all-in total of the option the customer asked for, pickup included | `engine.combine` |
| Q6 | Balanced = price as a multiple of the best price plus speed as a multiple of the best speed, equal weights, among bids only (fits six scored door-to-port rounds; not confirmed by 5U); the winner then competes by the customer's wish | `selection.balanced`, `selection.select` |
| Q7 | We quote in the chosen rate's own currency and compare in EUR with the rate's exchange rate | `selection.select`, `compose.render` |
| Q13 | The pickup zone is the first two digits of the postcode | `pickup.zone_of` |
