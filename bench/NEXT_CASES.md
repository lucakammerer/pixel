# Next cases

What new kinds of requirement the organisers are likely to add next, as of 22.11.2026. Since round 84 a round where
only 1 to 3 teams are accepted scores ×5, ×3 or ×2. Those rounds are the first appearance of a new requirement. So
far there were five, and we lost all of them: 10 (name the dangerous-goods documents), 15 (pickup without a place), 33 (packing list
contradicts the mail), 42 (customer excludes an airline), 76 (quote as PDF). A change only in wording, unit or format
makes no rare round: other teams read those with a model.

Each case below is a mail in `tests/fixtures/next-cases/`. Its expected answer, worked out by hand from the start kit, and
what the replay did today are in `tests/fixtures/next-cases.json`, with `known_gap` on every failing case. No test
reads either file, so neither one can block the deploy job. To replay one, offline and with no gateway spend:
`uv run python -m tools.replay tests/fixtures/next-cases/<case>.json`. With no tariffs the replay is not priced, so
the "today" column comes from replaying each case on a database that holds the start kit, the way `tools.kpi` does
it: `uv run python -m tools.kpi tests/fixtures/next-cases` prints price, carrier and route for every case. It also adds a missing row for each of the eight names in
`kpi-expectations.json`; skip those.

Rank = likelihood the organisers use it × how badly we fail today × how cheap the fix is (each 1 to 3). For a case
marked "model", the rules gave up and offline there is no model, so live behaviour is unmeasured.

| # | Case | Sample phrasing | Today | Required | Fix belongs in | Size |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `preferred-airline-only` | "We have a framework contract with Vega Skyfreight, so please quote Vega Skyfreight only" | **pass**: Vega 3221.50 | Vega 3221.50 | `extract.required_airlines`, `selection.select`, `bids.request` | — |
| 2 | `dims-per-piece-mixed` (live bug, not a new kind) | "- 2 pallets, 120 x 80 x 60 cm, 210 kg each" | **pass**: 600 kg, Vega 1920.00 | 600 kg, Vega 1920.00 | — | — |
| 3 | `latest-arrival-date` | "must arrive in Hong Kong no later than Monday 30 November" | **pass**: Helix 2132.80 | Helix 2132.80 | `extract.schedule`, `selection.meets_schedule` | — |
| 4 | `max-transit-days` | "Transit time must not exceed 3 days" | **pass**: Vega 1685.10 (3 d) | Vega 1685.10 (3 d) | `extract.schedule`, `selection.meets_schedule` | — |
| 5 | `ready-after-tariff-validity` | "ready on 4 December 2026" (sheets valid to 30.11.) | **pass**: November sheets excluded | a portal rate, bid or sheet valid on the ready date | `engine.sheet_options`, `session.for_request` | — |
| 6 | `customer-reference-in-quote` | "Please quote our purchase order reference PO-2026-4471 in your offer" | done | the reference in the quote and PDF | `compose.answers`, `pdf.reference` | S |
| 7 | `reply-in-other-language` | English mail: "Please reply in German, our shipping office does not read English" | **pass**: German | German (R37's cousin) | `extract.ASKED_LANGUAGE` | — |
| 8 | `door-to-door-dap` | "Terms are DAP … please include delivery from DXB airport to the door" | **pass**: HAM-DXB Orion 1029.00; the note on delivery at destination only from `novelty` (live) | HAM-DXB Orion 1029.00, says that delivery at destination is not included | — | — |
| 9 | `departure-airport-forced` | pickup 65189 Wiesbaden, "please route via Munich (MUC), not Frankfurt" | **pass**: MUC, Helix 1635.00 + pickup | MUC, Helix 1635.00 + pickup to MUC | `extract._asked_departure` | — |
| 10 | `incoterm-exw-implies-pickup` | "on EXW terms, ex works our plant at … 80809 München" | **pass**: door pickup 80809, MUC, Helix 1635.00 + pickup | door pickup 80809, MUC, Helix 1635.00 + pickup | `extract.DOOR` | — |
| 11 | `cost-breakdown-required` | "break the price down into air freight, fuel surcharge and security surcharge … all-in rate per kg" | **pass**: parts and 2.78 EUR per kg; greets "Hello Eva" | 1042.20 + 399.60 + 59.40 = 1501.20, 2.78/kg | `compose.breakdown`, `compose.signed_name` | — |
| 12 | `quote-validity-requested` | "the offer must be valid for at least 7 days. Please state how long your price is valid" | **pass**: "until 23:59 on Monday 30 November" | validity stated (sheet to 30.11.) | `calc.price_tariff` `valid_until` | — |
| 13 | `insurance-requested` | "goods are worth EUR 180,000. Please include cargo insurance" | **pass**: "Not included in this price: cargo insurance." | priced, or plainly not included | `compose.SERVICES` | — |
| 14 | `export-customs-included` | "Please include export customs clearance (EAD/ATLAS filing)" | **pass**: plainly not included | same | `compose.SERVICES` | — |
| 15 | `unknown-consignor-screening` | "We are not a known consignor, so the cargo will need security screening" | **pass**: plainly not included | same | `compose.SERVICES` | — |
| 16 | `currency-usd` | "please quote all-in in USD, not EUR" | 803.40 EUR | USD at the dataset's rate | `rfq/agent.py` `_fx_for`: a rate source when no option of the request is in USD | M |
| 17 | `cheapest-and-fastest-both` | "your cheapest and your fastest, each with its all-in price" | Helix 2132.80 only | Kestrel 1512.80 (4 d) and Helix 2132.80 (2 d) | `selection.select`, `compose.render` | M |
| 18 | `pickup-within-hours` | "The truck must collect within 24 hours of our order" | trucker by the balanced rule (offline: no bids) | only truckers ≤ 24 h compete | `selection.balanced` | S |
| 19 | `split-shipment` | "450 kg / 1.8 cbm ready on 27.11. and the remaining 300 kg / 1.2 cbm on 4.12. Two separate air waybills" | first part only, 1600.00 | 1600.00 + 1137.00 = 2737.00 | `extract.py` parts; each part priced in `rfq/agent.py` | M-L |
| 20 | `two-lanes-one-mail` | "Shipment 1: FRA to SGN … Shipment 2: STR to YYZ" | model; one `Rfq` per session | one reply: Orion 2380.00 and Cobalt 2340.00 | `Rfq` with several lanes, one quote (R4) | L |
| 21 | `non-stackable` | "NON-STACKABLE (fragile glass, nothing on top)" | 3.78 cbm, 2020.03 | assumed billed at 160 cm: 3.972 cbm, 2122.64 | `extract.py` flag, `calc.py` rule once the scorer's rule is known | M |
| 22 | `pickup-outside-zones` | "collected from our warehouse at … 5020 Salzburg, Austria" | **model**: no question; the extraction call names the nearest of our airports (MUC), air only | nearest served airport (MUC), pickup by bid | `extract._abroad_pickup`, `extract._departures_abroad` | — |
| 23 | `book-immediately-under-cap` | "If the all-in price is below EUR 1,500 … please book it straight away" | quote only | quote + booking + reference | `booking.py` intent from the request | M |
| 24 | `temperature-controlled` | "kept at +2 to +8 °C throughout, active temperature control required" | cheapest, silent | statement now; capable carrier once data names it | `compose.render`; carrier data | M |
| 25 | `quote-copy-to-colleague` | "please address the offer to my colleague Mr. Jens Hartmann" | **pass**: "Dear Mr. Hartmann," | greets Mr. Hartmann | `compose.addressee` | — |
| 26 | `customer-chargeable-stated-wrong` | "gross 380 kg … our own calculation is a chargeable weight of 400 kg" | 400 kg read as gross (by guess); price right here | 380 kg gross | `extract.py` | S |
| 27 | `direct-flight-only` | "Direct flights only please, no transfer at a hub" | wish ignored | direct options once data says so | data first | L |
| 28 | `oversize-freighter-only` | "320 x 150 x 25 cm … longer than 300 cm" | cheapest, silent | capable carrier once data names it | data first | L |
| 29 | `dg-lithium-in-body` | "lithium ion batteries, UN 3480, class 9" in the body only | **pass**: DGD and MSDS named | — | — | — |
| 30 | `not-dg-declared` | "this is NOT dangerous goods, no lithium, no UN number" | **pass**: no DG paragraph | — | — | — |

## Axis D: truckers and the pickup place

What the organisers can turn on the pickup leg next, as of 22.11.2026 03:30. Live so far: pickups with no place
(15 to 18), a zone not in the list (20, Nuremberg, too high), trucker offers only in a pdf, docx, txt or mp3 (63 to
67), a waiting rate per hour read as the price (67). A case with several mails is one fixture holding a list: the
request first, then each trucker's answer at its own time (`references` = the request), replayed `as_live` by
`tools.kpi` and `tools.replay` (`replay.raws_of`). Trucker prices are not in the start kit: each expected total is
the kit's airline from the zone's airport plus `selection.balanced` over the answers in the fixture, by hand.
Seen: live / escalation of a live lever / never.

| # | Case | Seen | Sample | Today | Required | Fix belongs in | Size |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | `trucker-pickup-time-in-days` | never | "Pickup time: 2 working days" | **fixed #329**: 2562.62 (before 2475.40, cheapest trucker, no hours read) | balanced on 24/72/48 h: 2562.62 | `bids._pickup_hours`, `calc.pickup_hours` | — |
| D2 | `pickup-postcode-beside-phone-number` | escalation (15 to 18) | "63450 Hanau" with "Tel. +49 6181 12345" in the signature | **fixed #334**: 2548.40 (before: rules gave up, offline unanswered) | 2548.40 | `extract.pickup_postcodes` | — |
| D3 | `pickup-postcode-abroad-matches-a-zone` | never | "20121 Milano, Italy" | **partial #334**: no longer zone 20 (before: HAM-SGN 2480.00, Hamburg truckers); now the model reads the origin | the scorer's airport for a pickup abroad (unknown) | `pricing/pickup.py` with next case 22 | M |
| D4 | `trucker-price-in-usd-without-rate` | escalation (A: desk in USD) | "for 152.30 USD", no rate | **fixed #337** once any source stated a USD rate (offline none: 2562.62) | 2520.12 at 0.92 | `bids.known_fx` | — |
| D5 | `trucker-all-silent` | escalation (A: airlines and desk silent, 93 to 97) | no trucker answers | 2380.00 airport-to-airport, pickup excluded, at 315 s: too low by the pickup | the zone's pickup priced | a learned zone price, proof needed first | M |
| D6 | `trucker-answers-late` | escalation | an answer at 160 s | 2562.62 at 100 s without it (1.7% high) | 2520.12 | `bids.collect` pickup wait; data on live answer delays first | S |
| D7 | `trucker-standard-and-express` | never | "Standard: 140.00 EUR, 30 hours / Express: 210.00 EUR, 4 hours" | model reads one; offline 2555.00 | each offer a bid: 2590.00 (assumption) | `bids.parse` one option per offer line | S-M |
| D8 | `trucker-base-plus-per-kg` | never (layouts state the split) | "base charge of 65.00 EUR plus 0.13 EUR per kg" | model call; offline 2590.00 | 2562.00 | `bids._price` sums base and per-kg | S |
| D9 | `trucker-asks-for-details` | never | "To quote we need the number of pallets" | a model call, no price; 2590.00 with the other trucker | the other trucker's price, or an answer to the question | `bids.py` | M |
| D10 | `trucker-condition-minimum-weight` | never | "This price applies from 1,000 kg" | **pass**: 2520.12 | 2520.12 | — | — |
| D11 | `pickup-zone-not-listed` | live (20) | "90461 Nuernberg", zone 90 not listed | rules give up; model guesses an origin, no trucker | unknown; round 20 lost too high from FRA | `pricing/pickup.py` zone fallback | M |
| D12 | `pickup-two-sites` | never (two shipments in one mail: 89, 90) | "500 kg in 63450 Hanau and 400 kg in 60314 Frankfurt" | rules give up; model takes one place | both pickups priced (assumption) | `Rfq` pickup legs, `pricing/pickup.py` | L |
| D13 | zone list or carrier list in a new version (no fixture) | never | a tariff mail with `pickup_zones.csv` that drops or moves a zone | `store.save_zones` / `save_carriers` merge: a dropped zone or trucker stays active | the new list replaces the old | `pricing/store.py` | S |
| D14 | trucker from an address not on the carrier list (no fixture) | never | a subcontractor answers for a listed trucker | `bids.record` treats an unknown sender as the rate desk: no price, no model | priced as that trucker's answer, or ignored and logged | `bids.record`, router | S |

Checked and passing beside the table: a pickup window ("only Tuesday 08:00-12:00"), "D-63450", an invoice,
head-office or consignee address beside the pickup postcode (#334), "plus 19% VAT" and "tail-lift 35.00 EUR extra"
beside the price, minutes ("90 minutes" is 2 h, #329), a pickup time in German. Not covered: "we collect tomorrow at
08:00" (a clock time, no duration), a pickup time only as a range of days.

## What the replay showed beyond the predictions

- The greeting took "Operations", a role line in the signature, as the name (11); fixed.
