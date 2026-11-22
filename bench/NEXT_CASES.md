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
| 31 | out of scope by mode (live, round 177, lost) | "port-to-port sea freight ... FCL in 1 x 40ft container", "DEHAM to AEJEA" | **fixed #465** for English and German wording with no word of air | the plain answer, no price | open, generalize: sea in another language (model read), courier or express, "air or sea, whichever is cheaper", sea-air, UN/LOCODEs only, a sea mail that says "air" somewhere | `orchestrator.out_of_scope` | S |
| 32 | latest arrival on a door request (live, round 179, lost too_high) | ready 27.11., "need to be at ORD by 2026-11-29", pickup in 51871 | **fixed #472**: the pickup adds no day, Aurelia (2 d) 1074.69 instead of Meridian (1 d) 1410.83 | the cheapest option with ready + transit ≤ the date | open, generalize: whether the scorer counts the ready day itself, a trucker's pickup time over 24 h, weekends, "arrive before" (exclusive) against "by" | `calc.arrival`, `selection.meets_schedule` | S |

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
| C15 | model text the mailbox refuses (no fixture) | the Turkish booking notice came back with control characters ("Say\x013n"), POST answered 500 (session 51345) | live (22.11. 08:15) | a translation with control characters is refused, the English mail goes out (#457); prose slots (`compose.prose`), the model-read greeting and a send 500 on other characters unchecked | every model-written text checked before it leaves, a send 500 answered with the plain English mail | `compose.localize`, `compose.prose`, `outbox._deliver`; open, generalize | S | 2×2×3 |
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
## Axis E: outage drills (22.11.2026 10:30, live snapshot to round 174)

`uv run python -m tools.outage_drill <database> --last 60`: each accepted round replayed offline on the state and
portal answers it met (`tools/capture.py`), every mail of its session at its own time (a change after our quote is
answered by a requote, the quote the scorer scores), the rate card as it is today, one dependency failing per drill.
The card's job reads every bracket of every portal lane (by 10:00 all 1341 of 206 lane and airline pairs), so the
session's own portal reads count as card rates. Right: a quote within 5 % of the scorer's best (`feedback.best`, else
our accepted total) within 6 minutes of storage. Offline every model call fails, so each drill includes the gateway
down. An airline or the rate desk asked by mail only because of the outage answers nothing (no such answer was
recorded): the portal cells are the worst case of rounds 93 to 99. `tests/test_outage_drill.py` runs every drill on
round 166.

| Drill | Right of 60 | Beyond the baseline | Cause | Fix belongs in | Owner |
| --- | --- | --- | --- | --- | --- |
| `gateway_down`, `gateway_hangs` (60 s, then a timeout) | 50 | baseline | 161, 149, 132 (Korean) and 171, 131 (Turkish): no quote, the rules read no destination or door pickup in them ("DFW (Dallas, US)까지", "ATL’ye", a postcode in a Turkish clause); 167 (Korean lots with "each"): the guess quotes 1964.76 against 4745.69; 168, 174: "we deliver to Düsseldorf Airport (DUS) ourselves", #463 open; 143, 139: a crosscheck against an attachment whose bytes the database does not hold (not measurable offline) | `extract.by_rules`, the guess path of `read_request` | Cost (`extract.py`), generalize |
| `portal_429` (every rate read 429, `Retry-After: 60`) | 48 | 2, as `portal_down` (E4) | was 3 of 60: a 429 that outlasted the deadline (`RateLimited`) never asked the rate card; `portal_rates._read` prices from it since #462 | — | — |
| E4 `portal_down`, `portal_nonsense`, `portal_hangs` (API and page), `airlines_silent` | 48 | 2 | 172 and 152: the change after our quote gets no requote; the quote before it was right | the requote path (`agents/rfq/agent.py`), cause not yet read | Accuracy |
| `mail_twice`, `mail_late` (90 s) | 50 | 0 | — | — | — |
| E3 `truckers_silent` | 49 | 1 | round 108: the pickup memory holds no answer of that zone within 24 h (D5's open part) | `pickup.estimates` | Accuracy |

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

## Axis R: the scorer's checks (22.11.2026 10:40, live database to round 179)

What the scorer reads from our first reply beyond the price, reconstructed from every `feedback` event (172 rounds,
8 to 179) and the request and reply of each loss. Counts are rounds, not events.

### The rubric, as far as the verdicts prove it

| # | Check | Reason when it fails | Proof | What it reads |
| --- | --- | --- | --- | --- |
| 1 | a reply in the thread before the request expires | `no answer` (6: 69, 86, 87, 89, 90, 107) | — | any mail with `in_reply_to` |
| 2 | the reply is in the customer's language | `wrong_language` (3: 70, 72, 156) | 156: an English question, then an English no-price reply to a Korean mail failed on language, not as a decline, so this check runs before 3 | the reply's text, also a no-price reply |
| 3 | an in-scope request gets a price | `declined_in_scope` (3: 96, 104, 122) | all three: our "could not obtain a freight rate" reply. A question is not scored: the simulated customer answers it within about 11 s and the quote after it counts (103, 131, 132 accepted; 82, 113 scored on price and count). 104 lost because the answer ("pickup from 40043, select the departure airport") was not priced | a price in the first reply that holds one, or a no-price reply |
| 4 | an out-of-scope request gets no price | `out_of_scope_quoted` (1: 177, sea FCL); the plain decline of an import was accepted (159) | — | a price in the reply |
| 5 | the offer is attached as a PDF that holds the price, when asked | `no_pdf_offer` (4: 76 to 79) | 78: a PDF without `%%EOF` counted as none | the attachment, parsed |
| 6 | every shipment of the mail has its own price | `one_of_two` (2: 113, 150) | both: two numbered shipments, "a separate all-in price for each"; we priced the first | the number of totals |
| 7 | an airline the customer ruled out is not offered | `excluded_airline` (1: 42) | — | the airline named in the reply |
| 8 | dangerous goods the documents reveal are answered with the documents they need | `dg_docs_missing` (1: 10) | the mail said "non-dangerous", the packing list `PL_53839.xlsx` said otherwise | named documents (DGD, MSDS) |
| 9 | the total within 5 % of the scorer's best, both ways (owned by the price goal since 09:10) | `too_low` (18), `too_high` (10) | accuracy falls on both sides: round 13 was 2.67 % below the best and earned 33 of 45 (about 4.5 points per %). With several shipments its `quoted` is the sum of all (62, 127, 151, 153, 158, all accepted): it reads more than one amount. Insurance terms stated in the request are in its all-in (91, `too_low`) | the all-in total, in EUR |
| — | the airline and the trucker are **not** compared | — | 172: our airline differed from the scorer's at the same total, 45 of 45; 13: another trucker, accepted | — |

Order: 1, then 2 before 3 (round 156). Where 5 to 8 stand against 9 is unproven: every loss on 5 to 8 had a price we
cannot place against the scorer's best (rejected rounds carry no `best`).

### The timeline and its pattern

| Round (time UTC) | New check | Stayed | Next check after |
| --- | --- | --- | --- |
| 10 (21.11. 16:00) | 8 DG documents, from an attachment | 1 round | 3 h |
| 42 (19:10) | 7 excluded airline | 1 | 3 h |
| 70 (22:00) | 2 language (French, then Chinese 72) | 2 rounds of 3 | 37 min |
| 76 (22:37) | 5 PDF | 4 in a row (76 to 79) | 2 h |
| 96 (00:43) | 3 decline, while the rate API was down (93 to 99) | 96, 104, 122 | 1.7 h |
| 113 (02:23) | 6 two shipments | 113, 150 | 6.5 h |
| 177 (08:50) | 4 another mode of transport | 1 so far | — |

The pattern: every new check is one more field of the generator's request (an attachment fact, an exclusion, a
language, an attachment wish, a shipment count, a mode), introduced alone in one round, repeated for 1 to 4 rounds, and
from then on **combined** with older ones: 156 is Korean + an "import" by the buyer + a forwarded old mail + PDF; 179 is
pounds and cubic feet + an arrival deadline + the buyer's "import" + PDF. Price levers move the same way from endpoint
to endpoint (pickup place 15 to 20, trucker offers in files 63 to 67, rate API 93 to 99 and 120 to 123). A new
check came every 0.6 to 6.5 hours; the last one is 2 hours old at this writing, so the next is due.

### The 20 most likely next checks

Likelihood: H = the generator already writes the field into every request or a check of the same kind is live; M = a
cousin of a live check; L = the dataset holds nothing the scorer could check it against.

| # | Check (predicted reason) | Likelihood | Why | Case | Today |
| --- | --- | --- | --- | --- | --- |
| P1 | the currency asked for (`wrong_currency`) | M | every request says "all-in price in EUR": one word of the template; but no live source ever priced in another currency (0 of 179 requests, 1223 carrier answers and every portal option), so the scorer would first need a rate it can hold us to | `currency-usd` | **wrong**: 803.40 EUR; blocked on data: `_fx_for` takes only this request's own rates, offline and live there is none |
| P2 | cheapest and fastest both (`one_of_two`) | H | `one_of_two` counts totals; "both options" is its next turn | `cheapest-and-fastest-both` | **wrong**: fastest only |
| P3 | two lanes in one mail (`one_of_two`) | H | 113 and 150 had one origin; the next turn varies the lane | `two-lanes-one-mail` | model (rules give up offline) |
| P4 | two pickup sites (`one_of_two`) | M | the same check on the pickup leg | `pickup-two-sites` | **wrong** offline: unanswered |
| P5 | a split shipment on two dates (`one_of_two`) | M | the same check by date | `split-shipment` | **wrong**: first part only |
| P6 | a latest arrival date (`late_arrival`) | H | 179 already writes "need to be at ORD by" and "price decides" | `latest-arrival-date` | right |
| P7 | a maximum transit (`late_arrival`) | H | same field, other unit | `max-transit-days` | right |
| P8 | a required airline (`required_airline`) | H | mirror of 7 | `preferred-airline-only` | right |
| P9 | the customer's reference echoed (`missing_reference`) | M | 5 reads the PDF; a reference is the cheapest field to check in it | `customer-reference-in-quote` | right |
| P10 | the customer's answer to our question priced (`declined_in_scope`) | H (live: 104) | the simulated customer answers every question; 104's answer named a pickup postcode instead of an airport | — (live 104) | owned by `extract.py` (Cost); 103's code path covers the earlier-request reference |
| P11 | DG named in the body only (`dg_docs_missing`) | M | 10's next turn moves the fact from the attachment to the body | `dg-lithium-in-body` | right |
| P12 | a language asked for that is not the mail's (`wrong_language`) | M | 2's next turn | `reply-in-other-language` | right |
| P13 | a breakdown asked for (`missing_breakdown`) | M | the PDF check already parses the reply | `cost-breakdown-required` | right |
| P14 | a validity asked for (`missing_validity`) | M | same | `quote-validity-requested` | right |
| P15 | pickup within N hours (`too_high`/`too_low` on the trucker) | M | the trucker is balanced on hours already | `pickup-within-hours` | **wrong**: hours not a filter |
| P16 | "book it at once below X" (`not_booked`) | M | 88 (cancel) shows the scorer reads the flow after the quote | `book-immediately-under-cap` | **wrong**: quote only |
| P17 | a stated chargeable weight that is wrong (`too_high`) | M | a template field the generator can lie in, like the packing list of 33 | `customer-chargeable-stated-wrong` | right by guess |
| P18 | a pickup abroad or in a zone not listed (`declined_in_scope`/`too_high`) | M (live: 20) | the pickup leg's next turn | `pickup-outside-zones`, `pickup-zone-not-listed`, `pickup-postcode-abroad-matches-a-zone` | model; offline unanswered |
| P19 | insurance, export customs, screening (`service_missing`) | M | the "include …" wishes the template already has for pickup | `insurance-requested`, `export-customs-included`, `unknown-consignor-screening` | right if the scorer accepts "not included" (unknown) |
| P20 | temperature control, oversize, direct flights, non-stackable (`no_capable_carrier`) | L | `carrier-list.csv` holds no capability column; the scorer has nothing to compare against until a new version adds one | `temperature-controlled`, `oversize-freighter-only`, `direct-flight-only`, `non-stackable` | silent; becomes **wrong** the hour the data names it |

### The table: every next case, 22.11. 12:00

`uv run python -m tools.checklist` replays every case of `next-cases.json` as `tools.kpi` does (start kit, no model)
and holds the first reply that prices or declines against the checks its `expected` names: a price at all (3), the
currency, one amount per option (6), words it must or must not hold (8, 7), greeting, language (2), and the attached
PDF with every expected total (5). Exit 1 on a wrong
case without a known gap. `-v` prints each reply; `body_states` was judged by hand on it.

| Case | Check | Today | Verdict | Gap owner |
| --- | --- | --- | --- | --- |
| `currency-usd` | P1 currency | 803.40 EUR | wrong, blocked on data | Checklist: watch for the first non-EUR source |
| `cheapest-and-fastest-both` | P2 two options | Helix 2132.80 only | wrong; fixed by the open pull request "two options" | Checklist (review thread merges) |
| `combo-de-two-options-pdf` (new: German, two options, PDF) | P2, 2, 5 | Helix 2132.80 only, German, PDF without Kestrel's total | wrong; fixed with #479 | Checklist |
| `combo-arrive-by-pdf-reference` (new: arrival, PDF, "our purchase order PO-2026-5512") | P9 reference | Helix 2132.80 right, PDF right, the reference not read | wrong; #481 open (also "Bestellung", "commande n°") | Checklist |
| `cheapest-and-fastest-one-option` (new, the opposite of P2) | one amount | one amount | right | — |
| `split-shipment` | P5 count | 1600.00, one amount | wrong | Cost (`extract.shipments`) |
| `two-lanes-one-mail`, `pickup-two-sites`, `pickup-outside-zones`, `pickup-zone-not-listed`, `pickup-postcode-abroad-matches-a-zone` | 3 a price | offline a no-price reply after the failed model call | unknown (model) | Cost (`extract`), Accuracy (`pricing/pickup.py`) |
| `book-immediately-under-cap` | P16 booked | quote only | wrong | After the quote (`booking.py`) |
| `door-to-door-dap` | delivery at destination stated | silent offline; live only `novelty`'s model note | unknown (model) | Checklist: `compose.SERVICES` next cycle |
| `temperature-controlled`, `oversize-freighter-only`, `direct-flight-only` | P20 | cheapest, silent | unknown (no data to check against) | Checklist: watch `carrier-list.csv` per version |
| `insurance-requested`, `export-customs-included`, `unknown-consignor-screening` | P19 | "Not included in this price: …" | right if "not included" passes (open question); with stated insurance terms the premium is priced (91) | — |
| `quote-validity-requested`, `cost-breakdown-required`, `customer-reference-in-quote`, `quote-copy-to-colleague`, `reply-in-other-language`, `dg-lithium-in-body`, `not-dg-declared` | P9, P11 to P14 | stated as expected | right | — |
| the other 21 cases | price and choice | as expected or within 5 % | right (price goal) | — |

Wrong outside a known gap: 0.

### Steps in every direction from the live checks

| From | Step | Case or proof | Today |
| --- | --- | --- | --- |
| 5 PDF | opposite: no PDF asked | every quote carries one; 123 accepted rounds had it | right |
| 5 PDF | another language: Korean ask (156) | the PDF keeps only the figures in a script Helvetica cannot write (`pdf._lines`) | holds the total |
| 5 PDF | two at once: PDF and several shipments | the PDF holds the mail's text, so every shipment's line and the sum | right (by reading `pdf._lines`) |
| 5 PDF | another moment: asked in a later mail | the requote carries its own PDF (`outbox.send_quote`) | right (by reading) |
| 6 count | another axis: two options instead of two shipments | `cheapest-and-fastest-both` | fixed in the open pull request |
| P9 reference | another wording, two at once: "our purchase order PO-…" with an arrival limit and a PDF | `combo-arrive-by-pdf-reference` | wrong, #481 |
| P9 reference | another language: "Bestellung 4500012345", "commande n° CMD-2231" | `tests/test_compose.py` | wrong before #481 |
| P9 reference | opposite: "PO Box 1234", "in order to ship 450 kg", "Die Bestellung wird am 27.11. abgeholt" | same | no reference |
| 6 count | opposite: "the cheapest and fastest option" (one option) | `tests/test_compose.py` `test_two_options_only_when_both_are_asked_for` | no second option |
| 6 count | another language: German, French "two options" | same test | right |
| 6 count | two dates, two pickup sites, two lanes | `split-shipment`, `pickup-two-sites`, `two-lanes-one-mail` | wrong or model, owners above |
| 7 excluded | opposite: one airline required | `preferred-airline-only` | right |
| 7 excluded | two at once: excluded airline and two options | `selection.select` filters by `allowed` for both | right (by reading) |
| 2 language | another party: a no-price reply or question in English to a Korean mail (156) | `wrong_language` live | open: `orchestrator._no_price_reply` text not translated; generalize |
| 2 language | language asked differs from the mail's | `reply-in-other-language` | right |
| 3 decline | another moment: the customer's answer to our question (104) | the answer named a pickup postcode, not priced | open, Cost (`extract`) |
| 8 DG | another channel: DG only in the body | `dg-lithium-in-body` | right |
| 8 DG | opposite: "NOT dangerous goods" | `not-dg-declared` | right |

### Open questions about the scorer

| Question | Cheapest live evidence |
| --- | --- |
| Does a reply that names a requested currency but quotes EUR fail, or is EUR always accepted? | the first request that asks for another currency, read with `tools.board_me` |
| Do checks 5 to 8 run before the price, i.e. would a right price with no PDF still be `no_pdf_offer`? | already in the database if a `no_pdf_offer` round had a price we can place: 76 to 79 against the board's `best` of other teams (not exposed) |
| Is "not included" an accepted answer to insurance, customs or screening wishes? | the first round with such a wish |
| Does the scorer accept one reply with both options for "cheapest and fastest", and which total does it compare? | the first such round; with several shipments it read the sum, so it reads more than one amount |
| Was 156's `wrong_language` for the question or for the no-price reply? | the next foreign-language round that ends without a price |

## Axis S: scope (22.11.2026, cycle 1)

Does a mail get the kind of answer it should: a quote, or a plain answer without a price (or none for a mail that
asks for nothing)? Round 177 priced a sea freight request as air (`out_of_scope_quoted`); rounds 96, 104 and 122 sent
a request we could have priced the answer without a price (`declined_in_scope`). The scorer tests both directions,
so half the cases are the in-scope side. 65 mails in `tests/fixtures/next-cases/scope/`, the expected answer of each,
written by hand from the challenge text and the domain, with how sure it is (`live`: seen scored, `text`: follows
from the challenge text, `guess`), in `tests/fixtures/next-cases/scope.json`. No test reads either.
`uv run python -m tools.scope_cases` replays each as live on the start kit, offline, and prints this table; exit 1 on
a wrong case without a known gap.

Branches: `mode` another mode of transport, `direction` import, cross-trade, domestic, `service` a service without
the flight, `none` no request at all, `partial` air beside something we do not do, `lookalike` in scope although it
looks out. "no_price after 0 s": a scope rule declined at once; "after 315 s": no rate was found and the fallback
answered at `hard_at` (right for the scorer, late for time points). `model`: the rules gave up and the model would
read it live; offline there is none, so today's answer is unmeasured.

| Case | Branch | Expected (how sure) | Today | Verdict | Known gap |
| --- | --- | --- | --- | --- | --- |
| `sea-fcl-locode` | mode | no_price (live) | no_price after 0 s | right |  |
| `sea-lcl-german` | mode | no_price (text) | no_price after 0 s | right |  |
| `sea-spanish` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-french` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-locodes-only` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-vessel-cutoff` | mode | no_price (guess) | no_price after 315 s | model |  |
| `rail-china` | mode | no_price (text) | no_price after 0 s | right |  |
| `rail-german` | mode | no_price (guess) | no_price after 315 s | model |  |
| `road-ftl` | mode | no_price (text) | no_price after 315 s | model |  |
| `road-groupage-german` | mode | no_price (guess) | no_price after 0 s | right |  |
| `courier-parcel` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-air-dubai` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-from-airport-company` | mode | no_price (guess) | no_price after 315 s | model |  |
| `sea-switch-from-air` | mode | no_price (guess) | quote | wrong | scope: `NOT_AIR` is lifted by any air word, here the past ("last month by air") |
| `import-explicit` | direction | no_price (live) | no_price after 315 s | model |  |
| `import-implicit` | direction | no_price (text) | quote | wrong | scope: no import said, `served_airports` turns PVG-FRA round to FRA-PVG and quotes it |
| `cross-trade` | direction | no_price (text) | no_price after 315 s | right |  |
| `domestic` | direction | no_price (guess) | no_price after 315 s | right |  |
| `customs-only` | service | no_price (text) | quote | wrong | scope: no field for what is asked; a service without the flight is read as an air request |
| `warehousing-only` | service | no_price (text) | no_price after 315 s | model |  |
| `insurance-only` | service | no_price (text) | quote | wrong | scope: no field for what is asked; a service without the flight is read as an air request |
| `truck-to-airport-only` | service | no_price (guess) | no_price after 315 s | model |  |
| `packing-only` | service | no_price (guess) | no_price after 315 s | model |  |
| `complaint-damage` | none | no_price (text) | quote | wrong | scope: router rule 7 (a weight or two codes in a new thread) makes a complaint a request |
| `invoice-query` | none | no_price (text) | quote | wrong | scope: router rule 7 makes an invoice query a request |
| `supplier-advert` | none | no_price (text) | no_price after 315 s | model |  |
| `job-application` | none | no_price (text) | no_price after 315 s | model |  |
| `newsletter` | none | no_price (text) | quote | wrong | scope: router rule 7 makes a newsletter a request |
| `out-of-office` | none | no_price (text) | quote | wrong | scope: router rule 7 makes an auto-reply a request |
| `misdirected` | none | no_price (guess) | no_price after 315 s | model |  |
| `general-rate-card` | none | no_price (guess) | no_price after 315 s | model |  |
| `bank-details-change` | none | no_price (text) | no_price after 315 s | model |  |
| `air-or-sea-cheaper` | partial | quote (guess) | quote | right |  |
| `instead-of-sea-air` | partial | quote (text) | quote | right |  |
| `two-shipments-one-sea` | partial | quote (guess) | quote | model |  |
| `air-plus-customs-storage` | partial | quote (text) | quote | right |  |
| `sea-or-air-german` | partial | quote (guess) | quote | right |  |
| `company-seaside` | lookalike | quote (text) | quote | right |  |
| `street-seehafen` | lookalike | quote (text) | quote | right |  |
| `shipping-loosely` | lookalike | quote (text) | quote | right |  |
| `container-loosely` | lookalike | quote (text) | quote | right |  |
| `port-cities-no-codes` | lookalike | quote (text) | no_price after 315 s | model |  |
| `fob-air` | lookalike | quote (text) | quote | right |  |
| `cif-air` | lookalike | quote (text) | quote | right |  |
| `sea-mentioned-past` | lookalike | quote (text) | quote | right |  |
| `seefracht-zu-langsam` | lookalike | quote (text) | quote | right |  |
| `import-buyer-side` | lookalike | quote (live) | quote | right |  |
| `seaport-code-beside-airport` | lookalike | quote (guess) | quote | right |  |
| `ocean-in-product` | lookalike | quote (text) | quote | right |  |
| `road-freight-in-goods` | lookalike | quote (text) | no_price after 0 s | wrong | scope: `orchestrator.NOT_AIR` matches a mode said in passing ("road freight" naming the goods) and no air word stops it |
| `rail-in-goods` | lookalike | quote (text) | no_price after 0 s | wrong | scope: `orchestrator.NOT_AIR` matches a mode said in passing ("rail freight" inside the goods name) and no air word stops it |
| `reederei-sender` | lookalike | quote (text) | quote | right |  |
| `own-truck-to-airport` | lookalike | quote (text) | quote | right |  |
| `courier-delivers-to-us` | lookalike | quote (text) | quote | right |  |
| `express-loosely` | lookalike | quote (text) | quote | right |  |
| `sea-dept-signature` | lookalike | quote (text) | quote | right |  |
| `locode-airports` | lookalike | quote (guess) | no_price after 315 s | model |  |
| `vessel-in-goods` | lookalike | quote (text) | quote | right |  |
| `not-by-sea` | lookalike | quote (text) | quote | right |  |
| `export-to-importer` | lookalike | quote (text) | quote | right |  |
| `no-mode-german` | lookalike | quote (text) | quote | right |  |
| `sea-forwarder-asks-air` | lookalike | quote (text) | quote | right |  |
| `by-ship-inbound` | lookalike | quote (text) | no_price after 0 s | wrong | scope: `orchestrator.NOT_AIR` matches a mode said in passing ("by ship" for the inbound leg) and no air word stops it |
| `port-of-loading` | lookalike | quote (text) | no_price after 315 s | model |  |
| `by-rail-inbound` | lookalike | quote (text) | no_price after 0 s | wrong | scope: `orchestrator.NOT_AIR` matches a mode said in passing ("by rail" for the delivery to the airport) and no air word stops it |

65 cases: 31 right, 12 wrong, 22 model. Every wrong case is a known gap of this thread (scope), cause in the row. #469 (set-aside wording) turned `sea-mentioned-past`, `seefracht-zu-langsam` and `not-by-sea` right.

## Axis T: supply

Source census on the offline SQLite snapshot through event 18991, 22.11.2026 10:09 CET (scorer round 179).
Reproduce the per-mail, per-lane, per-read and per-round ledger with
`uv run python -m tools.supply_census <copy> > <private-scratch>/supply.jsonl`. The ledger joins the mail IDs in
`bid.received` and `option.found`, and the source file's mail ID in `tariff.ingested` and `tariff.failed`. It
includes the original route, including other or tracking, so a routing mistake can be investigated. The latency
below runs from the stored mail time to the first recorded price, tariff ingestion or confirmed booking.

| Incoming source | Mails | Produced a price, tariff or confirmed booking | Latency median / max | Used in a sent quote |
| --- | ---: | ---: | ---: | --- |
| Airline bid | 743 | 14 | 0.318 / 1.617 s | 6 by evidence mail ID |
| Trucker bid | 489 | 463 | 0.172 / 10.518 s | 109 unique matches by lane, carrier and pickup amount |
| Rate desk | 1 | 0 | — | 0 |
| Tariff route | 3 | 2 | 0.438 / 0.466 s | 1 by lane and version. The third mail was a trucker offer. |
| Booking confirmation | 117 | 109 | 0.147 / 0.996 s | Confirmation comes after the quote. |
| Tracking plus unclassified | 122 | — | — | No price attached by the service. |

Published options: 477 bids and 178 sheet prices. The portal ledger has 395 successful request-side option
events, 30 failures (24 unavailable, four not on the portal, two API errors), three fresh booking rates, 1,139
quiet-time rate reads, and 14 pauses. The 152 portal quotes join to a rate ID. A next-break query that failed
or did not improve the price left no event, and quiet-time reads have no rate ID or raw response in the log.
The ledger covers the recorded reads. Owner: supply for request-side read and quote provenance, generalize for outage drills.

The store has 205 lane rows across 12 source/version pairs, including 47 rows from mail. The quote archive has
172 rows (152 portal, 14 sheet, six airline bid). Of 111 quotes with pickup, 109 have a unique matching bid
before send and two have no match. Pickup provenance is inferred from the logged values because the quote
does not keep a separate pickup mail ID. The board has 179 scored rounds. The session join gives 163 a sent
quote in the archive, eight a session without a sent quote and eight no session. Owner: supply for the
provenance audit, speed for missing request/session evidence.

Of the 14 archived sheet quotes, 13 refer to the kit and one to an update received by mail. No chosen sheet
had a newer eligible mailed version in the remaining version rows before send. No selected offer with a linked
enquiry had a later recorded answer from the same sender before send. These screens omit overwritten store
rows, unlinked bid answers and updates with no successfully read lane. An offer with missing validity cannot
be checked for expiry. The snapshot contains no `option.failed` with `error=implausible`. The earlier
round-67 hourly-charge misread remains covered by `tests/fixtures/bid-plausibility.json`. A raw review of the
30 portal failures remains open. Owner: supply for source fields, generalize for outages.

| Case | Expected from the source | Today | Status and owner |
| --- | --- | --- | --- |
| T1. Dated airline bid, body (mail 7017) | 2,877.65 EUR, HAM–EZE, 859 kg enquiry, 3 days, good through 17:53 UTC | Amount, lane, weight and transit agree. The option used in the quote has no expiry. | Wrong, `bids.parse` drops validity. Owner: supply. |
| T2. Dated trucker bid, body (mail 511) | 119.01 EUR pickup to FRA with 30 h lead time, good through 15:22 UTC | Amount and pickup airport agree. This historical option has neither pickup hours nor validity. | Wrong in the snapshot. Current rules have a pickup-hours reader. The bid still needs expiry. Owner: supply. |
| T3. Desk answer, separate thread (mail 176) | Two FRA–JFK offers for 340 kg, 952.00 EUR / 4 d and 1,149.20 EUR / 2 d, both to 14:38 UTC; one airline off-portal | Routed to the desk handler, which logged that its session had no RFQ. No option or off-portal follow-up was recorded. | Wrong in the snapshot. Recheck current subject correlation and ambiguous simultaneous enquiries in `poller.session_of`. Owner: supply. |
| T4. Pickup offer only in a DOCX (mail 19518, round 69) | Resolve the reply to the pickup enquiry and read the enclosed offer | Reply to a human-sent pickup request routed as `attachment:tariff`, then failed with missing currency or validity. Round 69 scored no answer. | Wrong in the snapshot. The routing error is verified. Its effect on the verdict is unproven. Owner: supply. |
| T5. Monthly updates, PDF and XLSX (mails 19740 and 21396) | Use the lanes, rates, currency and exact dates stated in each file | PDF imported 21 lanes dated 21.11–31.12.2026 and supplied one quote. XLSX imported 26 lanes dated 01.11–31.12.2025 and changed no eligible lookup. Bodies name a monthly update. | Unknown source correctness. The database stores attachment metadata only. Compare both files before treating the 2025 year as a misread. Owner: supply. |
| T6. Booking confirmation, body (mail 409) | Confirm the booked carrier and lane with the stated references and 3,094.18 EUR | Confirmation records the matching booking and shipment references. The price and lane agree with the sent quote. | Right for the sample. Other booking behavior belongs to after the quote. |
| T7. Portal rate expiry and dataset version | Preserve the response's currency, FX, rate ID, transit, expiry and dataset version through send | The sent-quote join preserves the IDs and sees no expiry before send. The rate event is already parsed. | Unknown source correctness for the raw portal fields. Owner: supply. |
| T8. Partial or full replacement, including a lane withdrawal and a future start | A one-lane amendment leaves other lanes sellable. An explicit withdrawal removes only its named scope. A future version starts on its stated ready date. | The store has per-lane, dated rows. There is no withdrawal record in this snapshot. | Unknown until amendment, full replacement, withdrawal and future-date behavior run together in the sheet harness. Owner: supply. |
| T9. Two versions in one mail and correction of a correction | Keep both validity windows. Apply the latest applicable correction by lane at pricing and before send. | No observed example. Reuse of earlier priced options can omit re-pricing for same-day requests. | Unknown pending an as-of replay. Owner: supply. |
| T10. Currency change and surcharge per shipment | Label the currency, use its stated FX and apply a flat surcharge once before currency conversion | The logged sheet rows carry currency, validity and per-kg fuel/security fields. A flat surcharge has no field. | Unknown (model) for unknown layouts. Test a refused layout and every plausible stub, including one that silently interprets a flat amount per kg. Owner: supply. |
| T11. One-hour bid, different weight, or one reply to two enquiries | Keep the stated expiry and weight tied to each enquiry. A fixed total for a different weight cannot price this request. | The air and truck samples lose validity. The rules build a bid option with the RFQ's weight. Multi-enquiry correlation remains untested. | Wrong for expiry; unknown for different weight or two enquiries. Owner: supply. |
| T12. Airline changes name/address, an unknown airline or trucker replies, or a zone changes airport | Resolve the replying party to the actual enquiry and apply each effective carrier or zone change once | Current store keys carriers by name and zones by zone. Audit of rename, unknown sender and zone replacement is pending. | Unknown until carrier/zone updates are replayed. Owner: supply. |

Steps already covered by this census include air versus truck versus desk, body versus attachment, reply linked
to an enquiry versus a separate desk thread, and source received before the quote versus a fresh rate read for
booking. The open variations still need opposite cases, pickup and air, arrival just before the quote or after
it or after booking, body and file and later correction, another language and script, another unit, and a
combined change. Extend the existing bid, sheet and desk checks in `tools.benchmark_generalization` and
`tools.simulate`. The census and this table have no direct effect on the accepted quote rate.

| Does the scorer accept one reply with both options for "cheapest and fastest", and which total does it compare? | the first such round |

## Axis P: price against the scorer's best (22.11.2026 11:20, live database to round 179)

`uv run python -m tools.price_ledger <database>` prints the rows below from a copy of the live database: every
round lost on price or on accuracy points, and every accepted round today's code would quote otherwise (none today:
round 46 gives 1731.19 against 1731.20). Today = current `main` on the options the session had when its quote was
drafted, plus what today's code adds that the session lacked (rate card on an outage answer, pickup memory, the
insurance premium, today's reading by rules). A rejected round names no best price, only its side: `too_low` puts
it above quoted / 0.95, `too_high` below quoted / 1.05. "Past the bound" means today's total lies beyond that bound,
so it is within 5 % of the scorer's best for every best up to the range shown; it is not proof of the best itself.

**Headline: 28 rounds were lost on price, not 68** (18 `too_low`, 10 `too_high`; the 68 counted `feedback` events:
round 93 was written 40 times, once per 10 s poll from 00:21 to 00:28 UTC, round 94 three times). **Today 18 of the 28 move past the verdict's
bound with the source or rule that was missing then, 1 moves short of it (67), 9 do not move or cannot be
replayed offline.** Of the three accepted rounds that lost accuracy points with a session in the database, all three give the
scorer's best today.

| Round | Verdict | Quoted then | Today | Today passes if best in | Cause class | Fixed by / owner |
| --- | --- | --- | --- | --- | --- | --- |
| 93 | too_low | 488.30 Cobalt 4 d + Flinkwagen | 556.84 Fjordline 3 d (card) + Flinkwagen | 530.32 to 586.15 | option never had: portal 503, fastest airline missing | rate card #452 (supply) |
| 94 | too_low | 2248.49 Danube 5 d | 3406.80 Xenon 2 d (card) | 3244.57 to 3586.11 | same | rate card |
| 95 | too_low | 4919.98 Kestrel 4 d + Alpenblick | 5701.81 Tamarind 3 d (card) + Alpenblick | 5430.30 to 6001.91 | same | rate card |
| 97 | too_low | 2334.92 Orion 2 d | 3378.64 Borealis 1 d (card) | 3217.75 to 3556.46 | same | rate card |
| 98 | too_low | 4949.69 Yellowtail 3 d + Hansekogge | 6646.27 Riverbend 1 d (card) + Hansekogge | 6329.78 to 6996.07 | same | rate card |
| 99 | too_low | 4149.10 Wayzata 4 d + Sternmeile | 5870.96 Ultraviolet 2 d (card) + Sternmeile | 5591.39 to 6179.96 | same | rate card |
| 120 | too_low | 3160.07 Helix 4 d + Spessart | 4196.52 Tamarind 2 d (card) + Spessart | 3996.69 to 4417.39 | same | rate card |
| 121 | too_low | 2692.21 Orion 4 d | 3483.39 Lumen 2 d (card) | 3317.51 to 3666.73 | same | rate card |
| 123 | too_low | 3512.14 Orion 4 d | 5196.54 Polaris 3 d (card) | 4949.09 to 5470.04 | same | rate card |
| 28 | too_high | 416.16 Quetzal sheet 2 d | 390.45 Ultraviolet bid / card 4 d | 371.86 to 396.34 | option had, not selected: airline bids balanced instead of by wish | #130 (selection), rate card |
| 63 | too_low | 2989.20 Ultraviolet, no pickup | 3262.40 + Westfalenroute (memory) | 3146.53 to 3434.11 | option never had: no trucker answered (offers in pdf, docx, mp3) | pickup memory #443 (supply) |
| 64 | too_low | 2803.53 Borealis, no pickup | 3068.98 + Sternmeile (memory) | 2951.08 to 3230.51 | same | pickup memory |
| 65 | too_low | 6828.36 Xenon, no pickup | 7276.64 + Westfalenroute (memory) | 7187.75 to 7659.62 | same | pickup memory |
| 91 | too_low | 927.95 Xenon 2 d | 1024.75 with insurance 96.80 | 976.79 to 1078.68 | priced differently: the premium on the request's terms belongs in the all-in | a10267d (`agent._insurance`) |
| 9 | too_low | 4739.50 Lumen at 1750 kg | 5177.96 Lumen at 1952 kg + Domblick | 4988.95 to 5450.48 | reading: three lots added up by the model | rules sum lots (#18, #21) |
| 82 | too_high | 45056.60 Polaris at 95 cbm | 2051.20 Polaris at 2.69 cbm | 1953.52 to 2159.16 | reading: "95.00 cubic feet" as cbm | febf549 |
| 118 | too_low | 4867.68 Nova Atlas 7 d + Kranich | 6634.32 Fjordline 4 d + Kranich | 6318.40 to 6983.49 | reading: ready date 26.11. not read, so the 7-day airline missed "arrive by 1.12." | ready date read today (the arrival rule of 179 does not change it: 26.11. + 7 > 1.12.) |
| 67 | too_low | 3345.48 Riverbend + Rheinufer 45.00 | 3515.72 Riverbend + Schwabenbote | 3521.56 to 3700.76: short of the bound by 5.84 | priced differently: "45.00 EUR for each hour" read as the trucker's price | `selection.implausible`; open: why 3515.72, the balanced total, lies below the bound (generalize: the trucker's real 181.96 / 28 h would not change the pick) |
| 179 | too_high | 1410.83 Meridian 1 d + Westfalenroute | 1074.69 Aurelia 2 d + Westfalenroute | 1023.51 to 1131.25 | option had, not selected: `calc.arrival` added a day for the trucker; the scorer counts ready + transit | hot repair A, on `main` (`calc.arrival`) |
| 130 | too_high | 3753.00 Aurelia at 1510 kg + Grautal | same offline | — | reading: the packing list said 1144 kg, the cross-check kept the mail's 1510 | likely 2ef42c6 (05:05 UTC, an hour after the round: a document's figure is never replaced by the cross-check); unproven, attachment bytes not stored; generalize |
| 33 | too_high | 1475.00 Borealis at 2.78 cbm | same offline | — | reading: document 1.92 cbm against the mail's 2.78 | #159; attachment not stored; generalize |
| 34 | too_high | 4980.00 Solace at 11.69 cbm | same offline | — | reading: document 7.49 cbm against the mail's 11.69 | #159, round 34 is a fixture; generalize |
| 35 | too_high | 8751.64 Nova Atlas 3 d | same offline | — | reading: document against mail (xlsx), which figure unknown | open, attachment not stored; generalize |
| 20 | too_high | 3723.64 Quetzal 2 d + Bodenseefracht | same | — | reading: pickup 90461 Nürnberg, zone 90 not listed, flown from FRA | open, D11; generalize |
| 16 | too_high | 1688.10 Helix 2 d + Bodenseefracht | same | — | reading: "our warehouse", place guessed (36627) | open, D2 class; scope of the pickup place: generalize |
| 15 | too_low | 1058.20 Polaris, no pickup | not comparable | — | reading: "our warehouse", no place: priced without pickup | open; today's reading uses the customer's later requests (24983, HAM), which the round did not have; generalize |
| 17 | too_low | 3844.65 Wayzata, no pickup | not comparable | — | same; the place was guessed (21663) | open; generalize |
| 18 | too_high | 2602.88 Juniper + Maintal at 21663 | not comparable | — | same; the customer wrote 51115 Bergisch Gladbach after our quote | open; generalize |
| 27 | accepted, 39.32 | 3018.37 Meridian bid 2877.65 + Hansekogge | 2980.72 Meridian card 2840.00 + Hansekogge | passes, best 2980.75 from the points | option never had: the portal's price during its 503, the airline's bid by mail 1.3 % above it | rate card |
| 13 | accepted, 33.0 | 2875.06 Borealis + Alpenblick | 2953.82 Borealis + Hansekogge | passes, best 2953.82 | trucker rule | `selection.balanced` (`times_the_best`) |
| 136 | accepted, 44.9 | 467.64 Borealis at 0.46014876 cbm | 467.53 at 0.46 cbm by hand (339.65 / 76.845 x 76.82 + 127.99) | passes, best 467.54 from the points | reading: a converted volume not rounded to 0.01 | 66b462b (`extract.CONVERTED`) |

Rounds 1, 2 and 4 lost accuracy points too; their sessions are not in this database.

### The scorer's rules as hypotheses, with counts

Counted over the 120 accepted rounds at 44.9 points or more that have a session (agree) and the 28 lost on price.
A contradiction is a lost round the rule alone explains and no other cause does.

| Rule we apply | Agree | Contradict | Rounds |
| --- | --- | --- | --- |
| The wish picks the airline: cheapest all-in, or fewest transit days with the cheaper on a tie | 120 (60 cheapest, 60 fastest) | 0 | the four cheapest rounds that did not quote the cheapest option are insurance (109, 164), a requote (151) and an arrival limit (170) |
| The trucker by `balanced` (`times_the_best`), its price added to the airline's | 84 | 0 | 130 would contradict only if the mail's 1510 kg were right; see the row above |
| Chargeable weight = max(gross, cbm x 167) | 47 | 0 | 14 lost rounds had volume weight above gross, each explained by another cause |
| Billed at a higher break when cheaper (portal, next break) | 18 | 0 | 33, 34 and 18 lost for their reading, not the break |
| The insurance premium on the customer's terms in the all-in | 3 (92, 109, 164) | 0 | 91 lost without it |
| Arrival = ready day + transit, no day added for the trucker (hot repair A; before: a day more with a trucker) | 2 (170; 179 today) | 0 | before the repair 179 contradicted the extra day |
| A converted unit rounded to 0.01 before pricing | 1 (136, by the points) | 0 | — |
| An airline bid or the rate desk as the price | 4 (29, 45, 46, 47) | 0 | — |
| Minimum on the freight, surcharges on billed weight, a quote in another currency | no scored round tests them | — | every sheet quoted above its minimum; no sheet quoted at a higher break with surcharges; every request asked EUR |

Steps sideways from round 179 (arrival from ready + transit): a weekend in transit, a ready date in the past, a
pickup longer than a day, the portal's transit against the sheet's, "by Friday". Only 170, 118 and 179 carry an
arrival limit, so none of these is tested by a scored round. Cheapest live evidence: the next round with an arrival
limit and a door pickup whose ready day is a Friday, read with `tools.price_ledger`.

Open: 67 (the balanced total below the verdict's bound), 35 (which figure), 15 to 18 and 20 (the pickup place),
130/33/34 (proof needs the attachments, not stored). Nothing in this axis is wrong today that has no owner.
||||||| d5406de
| Does the scorer accept one reply with both options for "cheapest and fastest", and which total does it compare? | the first such round |
