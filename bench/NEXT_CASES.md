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
| 20 | `two-lanes-one-mail` | "Shipment 1: FRA to SGN … Shipment 2: STR to YYZ" | both lanes read (`extract.shipments`), both priced in one quote; offline the rules give up first (model) | one reply: Orion 2380.00 and Cobalt 2340.00 | `agent._with_shipments` | — |
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
| D5 | `trucker-all-silent` | escalation (A: airlines and desk silent, 93 to 97) | no trucker answers | **fixed** (pickup memory): the zone's truckers at the price their earlier answers give, once one of them answered in that zone (before: 2380.00 airport-to-airport, pickup excluded, at 315 s: too low by the pickup). Proof on the live database of 22.11. 06:43, every request priced only from the answers before it: 72 of 104 door-to-port requests estimated (the rest the first in their zone), pickup error median 0.3 %, 90th percentile 22 %, the balanced trucker of the real bids in 68 of 72; total within 5 % of the quoted total 69 of 69 (1 %: 57), of the scorer's best 52 of 52 (1 %: 44), against 11 of 69 and 8 of 52 with the pickup excluded; pickup hours one value per trucker and zone in all 103 pairs answered twice | the zone's pickup priced | `pickup.estimates`; open: a zone no trucker ever answered | — |
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

## Axis C: infrastructure

What else the organisers can turn once wording, documents and price sources are exhausted: the mailbox, the gateway,
the clock and our own process. None of it was seen live by 22.11. 02:30. The closest live lever is the rate API's 503,
first with a hint, then without one (rounds 93 to 97), so the next turn of it most likely hits another endpoint. A
failure here takes a request whole, and a stalled poller takes every request after it: such a round goes to the 1
to 3 teams that survive it (R39).

Each case is in `tests/fixtures/next-cases/infra/`: an API response, push or send sequence with its expected result,
and the unit test that reproduces it or "code reading" where none does. The fixtures sit in a subfolder because they
are not mails: `tools.kpi tests/fixtures/next-cases` would replay them. `tools.replay` cannot show any of them either:
it sets the stored time to now, serves no poll and fails every gateway call. That last part makes the offline KPI
itself case C5.

Rank = likelihood × how badly we fail today × how cheap the fix is (each 1 to 3). Seen: **live** (seen live),
**esc.** (the next turn of a lever seen live), **new** (never seen).

| # | Case | Sample | Seen | Today (main) | Required | Fix belongs in | Size | Rank |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C2 | `mailbox-send-503-burst` | POST /v1/mailbox/messages 503 for ~10 s as the quote leaves | esc. | two 503s in a row: `quote.missed send_failed` | sent again for 60 s; [#327](https://github.com/lucakammerer/pixel/pull/327) open | `outbox._deliver` | S | 3×3×3 |
| C1 | `poll-page-unreadable-mails` | `received_at` as RFC 2822 or epoch ms, reference `"<abc@host>"`, attachment without id | new | **fixed** ([#324](https://github.com/lucakammerer/pixel/pull/324), merged): the whole poll raised, the poller stalled for good | the mail mapped field by field, a mail without an id skipped | `mailbox.to_message`, `poll`; `poller.run` | S | 2×3×3 |
| C7 | `attachment-get-429` | GET of a PDF-only request's attachment: 429, then 200 | new (R7 × rounds 65-68) | 429 raises, the request is read without its cargo | waited out 3 times; [#333](https://github.com/lucakammerer/pixel/pull/333) open | `mailbox.attachment` | S | 2×3×3 |
| C3 | `received-at-skew` | `received_at` as naive CET read as UTC (an hour ahead), or an hour behind | new | ahead: no timer fires before expiry; behind: `quote.missed expired`, no answer | ahead: now; expired against a push or a complete poll: that evidence; [#331](https://github.com/lucakammerer/pixel/pull/331) open | `poller.bounded` | S | 2×3×2 |
| C4 | in `mailbox-send-503-burst` | the no-price reply of a lane no source prices meets a 503 (with axis A) | esc. × esc. | the R1 answer is lost | sent again after a 503; in #327 | `outbox._deliver` | S | 2×2×3 |
| C5 | `gateway-down-model-only` | gateway 503 / 429 / timeout for the whole request | new | 503: requests the rules read are quoted, model-only ones (Chinese, voice, unclear) get `quote.missed no_price`. A 60 s hang (replay probe): rules-read quoted at 100 s, the router's model call (docx, pdf) waits 60 s unbounded before the request reaches the orchestrator, model-only as with 503 | one retry inside the call's wait; a bound on `router.classify`'s call; a full outage stays unquotable for model-only requests | `llm.ask_json`, `router` | S | 2×3×2 |
| C6 | `gateway-model-unavailable` | 404 for gpt-4.1 (`PIXEL_MODEL_CHECK`), gpt-4.1-mini still served | new | model-only extraction fails as in C5 | one retry on `PIXEL_MODEL` | `llm.ask_json` | S | 1×3×3 |
| C11 | `out-of-order-visibility` | 91012 visible before 91011, 91011's push lost | new | 91011 is never polled again | every Nth poll from a few ids back; `ingest` drops duplicates | `poller.run` | S | 1×3×2 |
| C8 | `push-timestamp-format` | `X-Webhook-Timestamp` in ms or ISO | new | 401 / 500, the 1 s poll takes it: latency only | read like a stored time | `webhook.hook` | S | 2×1×3 |
| C14 | `large-attachment` | 9.5 MB, 400-page scan as the only document | new | unmeasured; parse unbounded, no Rfq at takeover | bounded parse; what is read by then counts | `extract._attachments` | M | 1×3×1 |
| C15 | `portal-all-down` | the lane list fails with the rates (live: rounds 46, 47), or a rate read drops its connection | live | **fixed**: before, neither asked the rate card; rounds 120 to 123 replayed on the live database priced the fastest airline from it either way | the airline priced from the card | `portal_rates.options`, `_read` | S | 2×3×3 |
| C16 | `version-change-in-outage` | the dataset version changes while the rate API is down | esc. | the card prices the old version: 11.8 % off in the one rate it met (`tools.card_proof`, `new version`), a lost round | a factor for the new version without a portal read | `rate_card` | M | 1×3×1 |
| — | pushes stop | the webhook subscription is dropped | new | covered: the poll is the safety net (`test_poller`) | — | — | — | — |
| — | push twice / out of order | same push repeated or an older one after a newer | new | covered: stored once by id (`test_webhook`) | — | — | — | — |
| — | poll 429 with a slower minimum | "one poll per 5 seconds" | live (22.11. 01:23) | covered: interval learned from the 429 | — | — | — | — |
| — | mailbox 429 on send | 60 mails per minute (R7) | new | covered: waited out (`test_outbox`) | — | — | — | — |
| — | restart with a request open | deploy or crash in a request's second minute | live (every deploy) | covered: `orchestrator.recover`, handler kept to `hard_at` (`test_recover_restarts_an_open_request`) | — | — | — | — |
| — | ready date near midnight | "ready today" at 23:30 UTC, 00:30 CET | new | not reproduced: which day the scorer takes is unknown | — | `extract` dates, `pricing/session` validity | ? | — |

Cost points in a gateway outage (R28): a failed call logs 0 USD, and an 18-minute window without any spend earns no cost
points for any of its quotes. A full outage therefore costs the window's cost points whatever we do. In a partial
one (C6, or a 429 on one model) the prose call on gpt-4.1-mini keeps the window above zero.

Open and unverified: C5, C6, C8, C11, C14 and C16 have no fix. C6, C8 and C11 come from code reading, C14 is not
measured, and the midnight case is not reproduced. The live effect of #327, #331 and #333 is unmeasured until each is
merged and the organisers turn that lever.
## Axis E: outage drills (22.11.2026 09:30, live snapshot to round 167)

`uv run python -m tools.outage_drill <database> --last 60`: each accepted round replayed offline on the state and
portal answers it met (`tools/capture.py`), the rate card as it is today without the session's own reads, one
dependency failing per drill. Right: a quote within 5 % of the scorer's best (`feedback.best`, else our accepted
total) within 6 minutes of storage. Offline every model call fails, so each drill includes the gateway down. An
airline or the rate desk asked by mail only because of the outage answers nothing (no such answer was recorded): the
portal cells are the worst case of rounds 93 to 99. `tests/test_outage_drill.py` runs every drill on round 166.

| Drill | Right of 60 | Beyond the baseline | Cause | Fix belongs in | Owner |
| --- | --- | --- | --- | --- | --- |
| `gateway_down`, `gateway_hangs` (60 s, then a timeout) | 54 | baseline | 161, 149, 132 (Korean) and 131 (Turkish): no quote, the rules read no lane or weight in them; 152: the change mail of a requote is read by the model; 139: the request stands in an attachment whose bytes the database does not hold (not measurable offline) | `extract.by_rules` for Korean and Turkish requests | Cost (`extract.py`), generalize |
| E1 `portal_429` (every rate read 429, `Retry-After: 60`) | 4 | 50 | `portal_rates._read` prices from the rate card only on `RatesUnavailable`; a 429 that outlasts the deadline (`RateLimited`) drops the airline, so the quote goes out from a sheet or bid (round 166: Danube 3111.28 against Aurelia 2913.96) | `portal_rates._read`: a `RateLimited` read priced from the card like an outage | Accuracy, pull request from generalize |
| E2 `portal_down`, `portal_nonsense`, `portal_hangs` (API and page) | 29 | 25 | the card never read the lane (round 141 HAM-RUH: no read before or since), so a fastest request gets a sheet airline (861.28 against 1182.28) or the cheapest a dearer one; the card reads only lanes requests asked for | `rate_card.run`: every lane of `/v1/portal/ports`, not only the requested ones | Accuracy |
| `mail_twice`, `mail_late` (90 s), `airlines_silent` (airlines and desk) | 54 | 0 | — | — | — |
| E3 `truckers_silent` | 52 | 2 | rounds 108 and 100: the pickup memory holds no answer of that zone within 24 h (D5's open part), the pickup goes out excluded or estimated low | `pickup.estimates` | Accuracy |

Not drilled yet: a tariff version changing within a round; booking and tracking down (no effect on the quote, axis B).

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


## Several shipments in one mail (22.11.2026, matrix)

Lost or held six times (89, 90 no answer; 113, 150 `one_of_two`; 127, 151 held to 100 s). The reading is a one-factor
matrix in `uv run python -m tools.simulate --family shipments` (cells and their known gaps in
`tests/fixtures/rfqs/simulate-known-gaps.json`); pricing and timing are regression tests in
`tests/test_rfq_review_regressions.py`. Open, with owner:

| Cell | Today | Required | Fix belongs in | Owner |
| --- | --- | --- | --- | --- |
| `lanes=same_lane` | second shipment not quoted | both | `extract.shipments` | Cost |
| `lanes=two_postcodes` (D12) | each line's postcode read, every shipment from the first one's airport | each from its own zone airport | `agent.handle` (`store.airport_of`) | Cost |
| `layout=paragraphs`, `table`, `also` | one shipment quoted | all | `extract.shipments` | Cost |
| `layout=subject` | one shipment quoted | all | `extract.shipments` | Cost |
| `layout=attachment_each`, `attachment_sections` | one shipment quoted | all | `extract.shipments`, `_shipment_sources` | Cost |
| `wishes=cheapest_fastest`, `exclude_one`, `arrive_by_one` | the mail's wish on every shipment | each shipment's own | `extract.shipments` | Cost |
| a lane no source prices | that shipment and the sum left out | unknown: the scorer prices every lane | `agent._with_shipments` | Accuracy |
| one lane's rate API 503, its truckers silent | not measured | each priced from the sheet, rate card or zone | `engine.options` | Accuracy |
| accept both, accept "the second one", accept one and decline the other | one booking per session, always of the first shipment's option (`booking.book`, `quotes.for_session`) | a booking per accepted shipment | `booking.py`, `crm/quotes.py` | Shared |
| a change of one shipment's weight | `agent.requote` prices one request | that shipment again, the others kept | `agent.requote` | Shared |
| a trucker's answer outside its thread (no `in_reply_to`) | `bids.record` takes the carrier's latest request: every answer lands on the last shipment, the first has no trucker and its door quote falls back without pickup | matched by weight to its own shipment | `bids.record` | Accuracy |
| `tools/replay.py --session` of a door mail for several shipments | the carriers' answers point at the live ids of our requests, so they land as above; offline neither quote is reproduced | the replay maps a stored answer's thread to its own new request | `tools/replay.py` | Speed |

Measured right: two, three and four shipments, a door pickup for all, bullets, German, a currency for all, by rules;
another origin per line, door and port, French, by the model's read of the first shipment. Bids per lane, a lane still pricing at the target and every
lane priced in one reply with the sum: tests above.
