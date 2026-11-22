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
| 22 | `pickup-outside-zones` | "collected from our warehouse at … 5020 Salzburg, Austria" | model | nearest served airport (MUC), pickup by bid | `pricing/pickup.py` zone fallback | M |
| 23 | `book-immediately-under-cap` | "If the all-in price is below EUR 1,500 … please book it straight away" | quote only | quote + booking + reference | `booking.py` intent from the request | M |
| 24 | `temperature-controlled` | "kept at +2 to +8 °C throughout, active temperature control required" | cheapest, silent | statement now; capable carrier once data names it | `compose.render`; carrier data | M |
| 25 | `quote-copy-to-colleague` | "please address the offer to my colleague Mr. Jens Hartmann" | **pass**: "Dear Mr. Hartmann," | greets Mr. Hartmann | `compose.addressee` | — |
| 26 | `customer-chargeable-stated-wrong` | "gross 380 kg … our own calculation is a chargeable weight of 400 kg" | 400 kg read as gross (by guess); price right here | 380 kg gross | `extract.py` | S |
| 27 | `direct-flight-only` | "Direct flights only please, no transfer at a hub" | wish ignored | direct options once data says so | data first | L |
| 28 | `oversize-freighter-only` | "320 x 150 x 25 cm … longer than 300 cm" | cheapest, silent | capable carrier once data names it | data first | L |
| 29 | `dg-lithium-in-body` | "lithium ion batteries, UN 3480, class 9" in the body only | **pass**: DGD and MSDS named | — | — | — |
| 30 | `not-dg-declared` | "this is NOT dangerous goods, no lithium, no UN number" | **pass**: no DG paragraph | — | — | — |

## What the replay showed beyond the predictions

- The greeting took "Operations", a role line in the signature, as the name (11); fixed.

## Axis B: booking, tracking and the flow after the quote (22.11.2026, 02:00 to 03:30)

What happens after our quote is not priced by the scorer, but it decides the tracking +10 on a booked shipment (R30),
R24 and the jury's view of the mails, and the organisers have turned this lever twice (round 86 changed the cargo,
round 88 cancelled the booking). Live: "seen", the next turn of a lever seen live: "escalation", else "never".
These cases are flows, not first mails, so they do not live in `next-cases/` (`tools.kpi` replays each file there as a
request): they are mails in `tests/fixtures/thread-followups.json` (`uv run python -m tools.followup_matrix`) and
scripted portals in `tests/test_booking.py`, `tests/test_router.py` and `tests/test_tracking.py`. Each row is listed in
`next-cases.json` under `axis_b` with its harness. Rank = likelihood × how badly we failed × how cheap the fix (1 to 3).

| # | Case | Live | Sample | Today (before) | Required | Fix belongs in | Rank | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | `expired-rate-during-rate-outage` | escalation (rate 503 since round 93 + 410) | customer accepts after 15 min while `/rates` answers 503 or 429 | `refresh` raised, handler crashed, booking `requested` for good, customer never told | book by mail with the airline | `booking.book` | 27 | fixed #323 |
| B2 | `cancel-after-booking-raised` | seen (round 88) | "Please cancel the booking" | portal cancelled, then `emit("booking.cancelled")` raised: unknown event kind, customer never told | cancelled and told | `core/events.py` `KINDS` | 27 | fixed #336 (URGENT) |
| B3 | `rate-id-unknown-or-taken` | escalation (desk and browser-page rate ids, #310) | booking answers 404 or 409 | `booking.failed`, nothing else | fresh rate and book, else by mail | `booking.book` | 18 | fixed #323 |
| B4 | `confirmation-only-attached` | escalation (content only in an attachment, rounds 65 to 68) | "please find our booking confirmation attached" | routed as a tariff | confirmed | `router.rules` | 18 | fixed #332 |
| B5 | `conditional-acceptance` | never (reply-side cousin of 23) | "If the all-in stays below EUR 3,200, please book it" | dropped as a thank-you; German "unter 3.200 €" split at "3." and booked whatever the cap | book when our total meets the cap, else a person | `followup.conditional`, `selection.within_cap` | 12 | fixed #328 |
| B6 | `confirmation-unlisted-address` | never | "Booking BK-… confirmed" from an ops address off the carrier list | new customer request: model call, bids, quote to a refused address | confirmed | `router.rules` | 12 | fixed #332 |
| B7 | `airline-refuses-booking` | never | "your booking could not be confirmed: no space" | carrier bid, booking `requested` for good | failed for a person now; book the next option by itself | `booking.handle_confirmation` | 12 | partial #332; auto-rebook open (Shared, `booking.py`) |
| B8 | `cancel-portal-down-or-mail-booking` | escalation (88 + booking outage) | cancel a booking made by mail, or `DELETE` answers 503 | `human.note`, customer not told | withdraw by mail with the airline, tell the customer | `booking.cancel`, `_cancel_by_mail` | 8 | fixed #339 |
| B9 | `booking-failed-customer-silent` | escalation | any `booking.failed` (no address, 422, transport error) | nobody tells the customer; tracking says "a colleague is on it" | a short notice in the thread, or a person within the round | `booking._failed` | 8 | open (Shared) |
| B10 | `booking-503-no-hint-portal-airline` | escalation (rates 503 lost their hint in rounds 93 to 97) | booking 503 with no address, airline not on the carrier list | `booking.failed` | ask the rate desk for the airline's address, then book by mail | `booking._airline_address`, `bids` desk ask | 6 | open (Shared) |
| B11 | `fresh-rate-dearer-than-quote` | never | 410, the fresh rate costs more than we quoted | booked at the new purchase price, customer told the quoted total | policy: book at a loss up to the 5 % band, else a person | `booking.book`, `selection` | 4 | open (decision) |
| B12 | `change-after-booking` | escalation (86 + 88) | "after the booking: it is 980 kg now" | a person | cancel and rebook at the new weight, quote it in reply (if the scorer scores it) | `followup.action`, `booking` | 4 | open; scorer rule unknown |
| B13 | `confirmation-unknown-reference` | never | "Booking BK-QQ7Z0001 confirmed", unlisted address, no PX- | customer request | confirmation, unmatched (an `error`, no quote) | `router.rules` | 4 | open, `known_gap` in the matrix (Speed) |
| B14 | `confirmation-before-201` | never (axis C) | the airline's confirmation lands before the portal's 201 | unverified: `handle_confirmation` finds the `requested` booking by session and confirms it, then the 201 may set it back to `booked` | keep `confirmed` | `booking.book` | 4 | open, unverified |
| B15 | `new-request-in-booked-thread` | never | "could you also quote STR to YYZ, 320 kg" after the booking | a person | unknown: R4 counts only the first quote per thread | — | 2 | open, matrix case |
| — | already covered | | second customer mail before our quote, accept with change, cancel before booking, a second "yes", thanks after the booking, tracking in the thread | `followup_matrix`, `test_booking` | | | covered |
| — | acceptance after a requote | escalation (86) | | `quotes.for_session` takes the newest sent quote and its rate id | | `crm/quotes.py` | | covered by reading, no test |
| — | tracking API down | — | | there is no tracking API: tracking reads our CRM (R23, R25) | | | | n/a |

