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
| 33 | the selection wish in a language no rule reads (live, round 186, lost not_what_was_asked) | "Bu gönderi için en hızlı seçeneği istiyoruz" | **fixed #508** (Turkish, Korean, Chinese, Japanese wording) and **#512** (the novelty call's `wish` where no rule read one) | the fastest airline | open, generalize: a wish only in an attachment the novelty call does not see, a wish in a sentence a rule consumed for another fact, the model's wish on the 16 live requests with none stated (unmeasured, no recorded `wish`) | `novelty.check`, `extract.stated_preference` | S |

## Reusing an earlier shipment's details (round 192)

The new request asks for cheapest and cites an earlier subject with "Fastest". #529 takes that selection
from the request's own words. The captured round now quotes Tamarind at 2854.83 EUR instead of the two-day
Elbflug at 3707.22 EUR. `tests/test_earlier_preferences.py` exercises either direction, a different sender,
an earlier shipment already booked, forwarded history, German, and Turkish with pounds and cubic feet together.

Two rules-only cases remain open (Cost, `extract._earlier`): a current word in quotes ("fastest") is removed by
the same pattern as the older subject, and a current wish only in an attachment is skipped by the earlier
shipment reader. The attachment's cross-check was disabled for this drill. Neither variation has a scored
outcome in the snapshot.

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
| C15 | a non-quote mail the mailbox refuses (no fixture) | the Turkish booking notice came back with control characters ("Say\x013n"), POST answered 500 (session 51345) | live (22.11. 08:15) | **fixed**: a translation with control characters is refused, the English mail goes out (#457). A mail surely not sent (a 503, no connection; a 4xx only for a translation, then in English) is sent once more; a lost response or another 5xx may have been stored, so it is never sent twice: its row stays as the guard and is settled (`outbox.SETTLED`), its `error` says so once, no `delivery_unconfirmed` at every restart. 51345's notice (a 500) stays lost: it may have arrived. `tests/test_outbox.py`, seven kinds of mail | — | `outbox._deliver`, `outbox.repair_receipts` | — | — |
| C16 | a model-written slot of the quote with a control character | `compose.prose` slots and the model-read greeting go into the quote unchecked | esc. of C15 | a 500 on the quote is sent again with the same text until `RESEND_S`, then the fallback | the slot refused like a translation (`compose.CONTROL`) | `compose.prose`; open, Cost | S | 2×3×3 |
| C15 | `portal-all-down` | the lane list fails with the rates (live: rounds 46, 47), or a rate read drops its connection | live | **fixed**: before, neither asked the rate card; rounds 120 to 123 replayed on the live database priced the fastest airline from it either way | the airline priced from the card | `portal_rates.options`, `_read` | S | 2×3×3 |
| C16 | `version-change-in-outage` | the new dataset version is hidden when the rate API or its page is unavailable | replay, round 56 | 5442.11 EUR in time against the scorer's 6158.54 (11.63% low); with one new read for another airline the fallback quotes 6167.74, with both new reads it quotes 6158.54 | quote within 5% and before the deadline; investigate an independent version signal | `rate_card`, Accuracy | M | 1×3×1 |
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
| `gateway_down`, `gateway_hangs` (60 s, then a timeout) | 50 | baseline | 161, 149, 132 (Korean), 171, 131 and 126 (Turkish): the rules read no destination, pickup, pieces or wish in them, #489 open reads all six as the model did; 167 (Korean lots with "each", "로트 1: 카톤 4개"): the guess quotes 1964.76 against 4745.69, open, Cost; 168, 174 fixed by #463; 143, 139, 178: a document whose bytes the database does not hold (not measurable offline) | `extract.by_rules`, the lots in Korean | Cost (`extract.py`), generalize |
| `portal_429` (every rate read 429, `Retry-After: 60`) | 49 | 2 (E4) | was 3 of 60: a 429 that outlasted the deadline (`RateLimited`) never asked the rate card; `portal_rates._read` prices from it since #462 | — | — |
| E4 `portal_429`, `portal_hangs` | 49 | 2 | 172 and 152: under a 429 or a hang the first request's rate reads land (from the card) after the customer's change; the requote quoted the cheaper price of the older weight (1585.45 against 2231.04). `portal_down` and `airlines_silent` are at the baseline: the drill scored the requote at a position the replay never reached (a first quote held as superseded) | `agent.requote`, #503 open (51 of 60 with it) | generalize |
| `mail_twice`, `mail_late` (90 s) | 50 | 0 | — | — | — |
| E3 `truckers_silent` | 49 | 1 | round 108: the pickup memory holds no answer of that zone within 24 h (D5's open part) | `pickup.estimates` | Accuracy |

Round 56 checks the rate version at the first request after a change (`tests/test_outage_drill.py`). Unavailable
rates with no observation of the new version are the known gap C16. The normal portal and a 503 after one or
two recorded reads of the new version are controls. The version's exact activation time is not in the snapshot.
The newer gateway drill through round 187 improves from 50 to 56 of 60 with #489. Round 167 still falls back
to the last Korean lot (407 kg and 3.62 cbm) and quotes 2140.07 EUR instead of the scorer's 4745.69. Its
three lots total 1223 kg, 9.2264 cbm and 13 pieces. Both single and duplicate delivery reproduce the gap and
send one quote. The English control with declared per-piece volumes passes. Owner: Cost (`extract._lots`).
Not drilled here: booking and tracking outages (no effect on the quote, axis B).

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

## Axis R: the scorer's checks (22.11.2026, live database to round 192)

What the scorer reads from our first reply, reconstructed from the latest `feedback` per round (185 rounds,
8 to 192: 134 accepted, 45 rejected and 6 with no answer) and the requests and replies behind the losses.

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
| 9 | the total stays within 5 % below the scorer's best (Price) | `too_low` (18) | round 13 was 2.67 % below the best and earned 33 of 45 (about 4.5 points per %). Insurance terms stated in the request count in the all-in (91) | the all-in total in EUR |
| 10 | the total stays within 5 % above the scorer's best (Price) | `too_high` (10) | round 179 quoted a 1-day service when a cheaper 2-day service met the limit. For several shipments the scorer uses the sum (62, 127, 151, 153, 158, accepted) | the all-in total in EUR |
| 11 | the option or reply fulfils the customer's stated wish | `not_what_was_asked` (2: 186, 192) | 186 asked for the fastest option and an arrival estimate. The reply offered the cheapest and omitted the estimate. In 192 the new mail asked for cheapest, quoted the old subject with "Fastest", and got the quickest option with an arrival date. The two rounds establish that choosing by the current wish matters; an independent arrival check remains unproven | the option against the current wish |
| — | the airline and the trucker are **not** compared | — | 172: our airline differed from the scorer's at the same total, 45 of 45; 13: another trucker, accepted | — |

Order: 1, then 2 before 3 (round 156). The order of 5 to 8 and 11 relative to the two price thresholds remains
unproven. Those losses carry no `best`.

### The timeline and its pattern

| Round (time UTC) | New check | Stayed | Next check after |
| --- | --- | --- | --- |
| 10 (21.11. 16:00) | 8 DG documents, from an attachment | 1 round | 3 h |
| 42 (19:10) | 7 excluded airline | 1 | 3 h |
| 70 (22:00) | 2 language (French, then Chinese 72) | 2 rounds of 3 | 37 min |
| 76 (22:37) | 5 PDF | 4 in a row (76 to 79) | 2 h |
| 96 (00:43) | 3 decline, while the rate API was down (93 to 99) | 96, 104, 122 | 1.7 h |
| 113 (02:23) | 6 two shipments | 113, 150 | 6.5 h |
| 177 (08:50) | 4 another mode of transport | 1 so far | 56 min |
| 186 (09:46) | 11 a wish only noted, in a language the rules do not read | 186 and 192 | — |

The pattern: every new check is one more field of the generator's request (an attachment fact, an exclusion, a
language, an attachment wish, a shipment count, a mode), introduced alone in one round, repeated for 1 to 4 rounds, and
from then on **combined** with older ones: 156 is Korean + an "import" by the buyer + a forwarded old mail + PDF; 179 is
pounds and cubic feet + an arrival deadline + the buyer's "import" + PDF. Price levers move the same way from endpoint
to endpoint (pickup place 15 to 20, trucker offers in files 63 to 67, rate API 93 to 99 and 120 to 123).

### Wishes we recognised and only noted (every `rfq.novelty` through round 192)

13 distinct sessions, 112 to 186, with a nonempty `mapped.conditions` or `mapped.documents`. Each row compares
the first offer and stored attachment receipt with the request and verdict. A session can appear in several rows.
Round 192 had no mapped novelty. Its new preference for cheapest lost to the quoted subject of an earlier
request for fastest. Hot repair A owns the request-reading fix.

| Kind of wish | Sessions (round) | Acted | Only noted | The scorer | Changes the right option or content |
| --- | --- | --- | --- | --- | --- |
| fastest or cheapest | 149, 167 (ko), 171 (tr), 186 (tr) | 149, 167 by the model's read; 171 cheapest by default | **186** | `not_what_was_asked` | yes: the option |
| truck to the airport | 118 (de), 132, 149, 161 (ko) | all: pickup priced | — | accepted (118 lost `too_low` on an arrival limit the rules then missed, read since) | yes, acted |
| we deliver to the airport | 137 (de) | port-to-port | — | accepted | yes, acted |
| among the dates that fit, the cheapest | 170 | cheapest within `arrive_by` | — | accepted | yes, acted |
| dangerous goods in the attachment | 112, 128 | DGD and MSDS named | the note too | accepted | yes, acted |
| PDF for filing | 161, 165, 167, 179 | receipt confirms the offer PDF in all four | 161 asks the customer for the quote and PDF. 167 asks for the PDF | 161, 165 and 167 accepted. 179 `too_high` | yes, the request for a document reverses who owes it |
| one air waybill, lots not split | 167 (ko) | priced as one | the sentence noted; `compose.ONE_AWB` answers only English and German | accepted | content |
| earliest flight and estimated arrival | 186 (tr) | transit duration given | neither question answered. The model output mapped the ready date and fastest wish | `not_what_was_asked`; the reason does not separate the two omissions from the option | yes, the arrival estimate |
| pieces, EUR, all-in | 132, 171 | in the quote | noted | accepted | no |

Ranked predictions of `not_what_was_asked` from the kinds only noted, each stepped in every direction:

| # | Prediction | Directions tested (offline, `extract`, `compose` on this commit) | Today | Fix belongs in | Owner |
| --- | --- | --- | --- | --- | --- |
| N1 | fastest in a language the preference rules miss | #508 covers Turkish, Korean, Chinese and Japanese. #512 applies the preference novelty reads when the rules found none. "Fastest, price decides" exercises the explicit priority. A later change belongs to Axis B | round 186 wording fixed by Hot repair A. Polish and other wordings remain model-dependent where rules miss them | `extract._preference` | Cost |
| N2 | earliest arrival omitted despite a known ready date | DE date before verb and short date, KO date before verb, EN month name all reach `extract.schedule` but miss `compose.ready_date`. TR needs the novelty date in round 186. The document can supply or override the mail date (see `test_an_attached_documents_schedule_is_read_and_stands_against_the_mails`). Reply-by alone supplies no readiness. A correction before the quote should use the corrected date | wrong where the composer misses an effective ready date. TR and KO can still lose the date in translation | `compose.answers` with the extracted date. A specific flight still needs schedule data | Checklist |
| N3 | the offer PDF ask becomes a request for the customer to send a document | stubs `["PDF", "견적서"]`, `["shipper's declaration", "PDF"]`, and empty. The carrier's bid PDF supplies sourcing data. A customer's real DGD in PDF form still needs to be requested when required | wrong content, accepted twice. Empty stub produces no request | give the note the direction of each document request | Checklist |
| N4 | a wish no option can meet | offline `agent._quote` with both a required airline and an impossible arrival gives both alternative notes when the RFQ carries the fields. The same inputs stubbed in novelty's `only_airlines` and `arrive_by` give neither note. A compatible airline and date correctly give neither note | model-only alternative disclosure open | check the selected option against the same requirements used for selection | Checklist (review thread merges) |
| N5 | one air waybill or separate air waybills | the answer renderer confirms English "One AWB for all lots". Korean round 167 gets a note only and French "une seule LTA" gets no sentence. The opposite "two separate air waybills" triggers a one-AWB promise | wrong confirmation for the opposite case. Korean and French confirmation missing | `compose.ONE_AWB` | Checklist |

Arrival was explicitly requested in 52 of the 179 first mails through round 186. In the offline replay of 186,
stubbing its recorded ready date and using the corrected fastest option gives an estimated arrival of 28 November.
The current composer leaves out that date. A candidate that uses `rfq.ready_on` also gives the same estimate in
the reply and PDF when the date is written out in the body, supplied only in an attachment, or supplied in an
attachment that supersedes the mail date. A reply-by date alone still produces no estimate. The quote's
translation remains model-dependent. A translation stub that keeps the price and transit but drops the date
passes `compose.translated_ok`. Changes to the ready date in a later mail are owned by Axis B
(`followup.changes` reads cargo and routing changes but no schedule fields).

### The 20 most likely next checks

Likelihood: H = the generator already writes the field into every request or a check of the same kind is live; M = a
cousin of a live check; L = the dataset holds nothing the scorer could check it against.

| # | Check (predicted reason) | Likelihood | Why | Case | Today |
| --- | --- | --- | --- | --- | --- |
| P1 | the currency asked for (`wrong_currency`) | M | every request says "all-in price in EUR": one word of the template; but no live source ever priced in another currency (0 of 179 requests, 1223 carrier answers and every portal option), so the scorer would first need a rate it can hold us to | `currency-usd` | **wrong**: 803.40 EUR; blocked on data: `_fx_for` takes only this request's own rates, offline and live there is none |
| P2 | cheapest and fastest both (`one_of_two`) | H | `one_of_two` counts totals; "both options" is its next turn | `cheapest-and-fastest-both` | right offline after #499 |
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

### The table: next cases on main, 22.11.2026

`uv run python -m tools.checklist` replays every case of `next-cases.json` as `tools.kpi` does (start kit, no model)
and holds the first reply that prices or declines against the checks its `expected` names: a price at all (3), the
currency, one amount per option (6), words it must or must not hold (8, 7), greeting, language (2), and the attached
PDF with every expected total (5). Exit 1 on a wrong
case without a known gap. `-v` prints each reply; `body_states` was judged by hand on it.

| Case | Check | Today | Verdict | Gap owner |
| --- | --- | --- | --- | --- |
| `currency-usd` | P1 currency | 803.40 EUR | wrong, blocked on data | Checklist: watch for the first non-EUR source |
| `cheapest-and-fastest-both` | P2 two options | Helix 2132.80 and Kestrel 1512.80 | right offline after #499 | — |
| `combo-de-two-options-pdf` (German, two options, PDF) | P2, 2, 5 | both totals in the German reply and its PDF | right offline after #499 | — |
| `combo-arrive-by-pdf-reference` (new: arrival, PDF, "our purchase order PO-2026-5512") | P9 reference | Helix 2132.80 right, PDF right, the reference not read | wrong; #481 open (also "Bestellung", "commande n°") | Checklist |
| `cheapest-and-fastest-one-option` (new, the opposite of P2) | one amount | one amount | right | — |
| `split-shipment` | P5 count | 1600.00, one amount | wrong | Cost (`extract.shipments`) |
| `two-lanes-one-mail`, `pickup-two-sites`, `pickup-outside-zones`, `pickup-zone-not-listed`, `pickup-postcode-abroad-matches-a-zone` | 3 a price | offline a no-price reply after the failed model call | unknown (model) | Cost (`extract`), Accuracy (`pricing/pickup.py`) |
| `book-immediately-under-cap` | P16 booked | quote only | wrong | After the quote (`booking.py`) |
| `door-to-door-dap` | delivery at destination stated | silent offline; live only `novelty`'s model note | unknown (model) | Checklist: `compose.SERVICES` next cycle |
| `temperature-controlled`, `oversize-freighter-only`, `direct-flight-only` | P20 | cheapest, silent | unknown (no data to check against: at 12:40 the live `carriers` table holds name, kind, email, serves only, and no request since session 50000 asked for any of it) | Checklist: watch `carrier-list.csv` per version |
| `insurance-requested`, `export-customs-included`, `unknown-consignor-screening` | P19 | "Not included in this price: …" | right if "not included" passes (open question); with stated insurance terms the premium is priced (91) | — |
| `quote-validity-requested`, `cost-breakdown-required`, `customer-reference-in-quote`, `quote-copy-to-colleague`, `reply-in-other-language`, `dg-lithium-in-body`, `not-dg-declared` | P9, P11 to P14 | stated as expected | right | — |
| the other 21 cases | price and choice | as expected or within 5 % | right (price goal) | — |

The offline checklist ran 45 cases. Four failed an asserted check (currency, split shipment, booking under cap,
PO reference) and five require model reads, all with owners above. No asserted check failed outside the known
gaps. A passing checklist result covers only the checked fields. The prose expectations and unpriced services
still require inspection.

### Steps in every direction from the live checks

| From | Step | Case or proof | Today |
| --- | --- | --- | --- |
| 5 PDF | opposite: no PDF asked | every quote carries one; 123 accepted rounds had it | right |
| 5 PDF | another language: Korean ask (156) | the PDF keeps only the figures in a script Helvetica cannot write (`pdf._lines`) | holds the total |
| 5 PDF | two at once: PDF and several shipments | the PDF holds the mail's text, so every shipment's line and the sum | right (by reading `pdf._lines`) |
| 5 PDF | another moment: asked in a later mail | the requote carries its own PDF (`outbox.send_quote`) | right (by reading) |
| 6 count | another axis: two options instead of two shipments | `cheapest-and-fastest-both` | right offline after #499 |
| 6 count | opposite: "Two pallets. Cheapest and fastest please.", "the fastest pickup for each shipment", "Both shipments: the cheapest and the fastest routing", a bare "cheapest and fastest options" | `tests/test_compose.py` on #499 | one total |
| P6 arrival | what the reply states: "the earliest arrival would be …" | `compose` takes `calc.arrival`, ready + transit since #472 (round 179) | the same date the scorer counts |
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
| Does check 11 require an arrival estimate when the option follows the wish? | the next scored request asking for a date where the reply already quotes the right option |
| Does a fulfilled PDF request still pass when the reply also asks the customer to send the PDF? | 161 and 167 passed. The next filing request combined with an actual document requirement would test the distinction |
| Was 156's `wrong_language` for the question or for the no-price reply? | the next foreign-language round that ends without a price |

## Axis S: scope (22.11.2026, through round 192)

Does a mail get the kind of answer it should: a quote, or a plain answer without a price (or none for a mail that
asks for nothing)? Round 177 priced a sea freight request as air (`out_of_scope_quoted`); rounds 96, 104 and 122 sent
a request we could have priced the answer without a price (`declined_in_scope`). The scorer tests both directions,
so half the cases are the in-scope side. 84 mails in `tests/fixtures/next-cases/scope/`; the expected answer of each,
written by hand from the challenge text and the domain, with how sure it is (`live`: seen scored, `text`: follows
from the challenge text, `guess`), and the step it was taken from (Luca's "look in every direction": language,
script, channel, moment, unit, two at once) in `tests/fixtures/next-cases/scope.json`. No test reads either.

`uv run python -m tools.scope_cases` replays each as live on the start kit, offline, and prints this table; exit 1 on
a wrong or split case without a known gap. Where the rules give up, the model reads the mail live; the tool stubs
its plausible answers instead of asking it: `air`, the reading as an air request written by hand in the case (the
nearest airports, as round 177's model did), and `blank`, a reading that names no airport; `routed other` where the
router asks the model. A case is right only when every plausible answer leads to the right kind, `split` when it
depends on the answer. `no_price late` means more than 60 seconds elapsed in the replay. The no-price reply may
be the deadline fallback or a scope decline delayed by a question. A question for a missing fact is allowed
before a quote. A request outside our scope should get the decline as its first reply. Cases marked `decline`
require a reply. `no_price` also permits silence for a mail that asks for nothing. Ten established freight and
service requests now require a decline in the replay. This exposes the routing gap for the attached sea request
as consistently wrong across the stubbed readings. It can quote, ask a question or stay silent.

The causes, all owned by this thread unless the row says otherwise:

- **G1**: scope is decided after `read_request`, whose question for a missing fact has already gone out.
- **G2**: there is no scope reading. The mode is said in words no rule knows (other languages, "ocean rates",
  "vessel", TEU, 20'GP, FTL, "per LKW", courier, sea-air) and the extraction schema has no field for it, so the
  model always fills in an air request.
- **G3**: any air word lifts the mode rule, also "Airport" in a company name and "by air" said of last month.
- **G4**: an import by lane alone is turned round by `served_airports` and quoted as the export the other way. Fix in review (#513).
- **G5**: a lane with neither end ours (cross-trade, domestic) is priced until `hard_at`.
- **G6**: a service without the flight (customs, insurance, storage, crating, a truck to the airport only) becomes
  an air request.
- **G7**: router rule `new_thread` makes every new thread a request; a complaint, an invoice query, a newsletter, an
  out-of-office reply gets a question and a price.
- **G8**: the mode rule reads subject and body, not an attachment.
- **G9**: when a customer puts the full freight details in an attachment, the router can classify the message as
  `other`. The customer then gets no reply even when extraction could read the attached lane.
- **G10**: `orchestrator.NOT_AIR` matches a mode said in passing ("by ship" for the inbound leg, "rail freight" in
  the goods' name, "our sea freight team"); an in-scope request is declined.

Proof against the other direction: `uv run python -m tools.scope_proof <copy of the live database>` replays every
stored customer request offline beside the scorer's verdict. It counts an explicit scope decline even when a
question went out first. A missing rate and a missing reply are separate outcomes. Model answers replay only if
recorded for the same prompt, and the database holds attachment metadata without the files. Every scope pull request
reports the count of previously accepted quotes that now get a scope decline or lose their reply.
On the snapshot through round 192, main and the simulated merge of #513 both replayed 119 of 133 previously
accepted quotes as quotes. The remaining 14 were unpriced in replay, with zero scope declines and zero missing
replies. Round 159 was accepted as a scope decline live and still declines. Round 177, rejected for quoting sea
freight, now declines. On the fixtures #513 declines both written imports, including the German request with
imperial units, while the matched export still quotes. The attachment import declines when routed as an RFQ.
The possible silence on that request belongs to G9.

The class fix (next pull requests, read path, for the review thread): decide scope before the question (G1), then
one scope reading: a rule that only notices a mode, service or non-request word in any language (subject, body and
attachment, G2, G6, G7, G8) hands the mail to the extraction call, which returns one more field, what is asked, and
code declines on anything but air export (G3, G10), no second call. Until then the rules stay as they are.

| Case | Branch | Step from | Expected (how sure) | Today | Verdict | Known gap |
| --- | --- | --- | --- | --- | --- | --- |
| `sea-fcl-locode` | mode |  | decline (live) | air: no_price; blank: question → no_price late | split | scope (G1): `agent.handle` decides scope after `read_request`, whose question has already gone out (a blank model reading) |
| `sea-lcl-german` | mode |  | decline (text) | air: no_price; blank: question → no_price late | split | scope (G1): `agent.handle` decides scope after `read_request`, whose question has already gone out (a blank model reading) |
| `sea-spanish` | mode |  | no_price (guess) | air: no_price late; blank: question → no_price late | split | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it |
| `sea-french` | mode |  | no_price (guess) | air: no_price late; blank: question → no_price late | split | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it |
| `sea-locodes-only` | mode |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (LOCODEs, 20'GP, CY/CY) |
| `sea-vessel-cutoff` | mode |  | no_price (guess) | air: no_price late; blank: question → no_price late | split | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it ('ocean rates', 'vessel') |
| `rail-china` | mode |  | decline (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G1): `agent.handle` decides scope after `read_request`, whose question has already gone out |
| `rail-german` | mode |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (Bahn, Schiene) |
| `road-ftl` | mode |  | decline (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (FTL, truck load) |
| `road-groupage-german` | mode |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (per LKW) |
| `courier-parcel` | mode |  | no_price (guess) | air: quote; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (courier parcel) |
| `sea-air-dubai` | mode |  | no_price (guess) | air: no_price late; blank: question → no_price late | split | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (sea-air) |
| `sea-from-airport-company` | mode |  | no_price (guess) | air: question → quote; blank: question → no_price late | wrong | scope (G3): any air word lifts the mode rule (`orchestrator.AIR`), here 'Airport' in the company's name |
| `sea-switch-from-air` | mode |  | no_price (guess) | quote | wrong | scope (G3): any air word lifts the mode rule (`orchestrator.AIR`), here 'by air' said of last month |
| `import-explicit` | direction |  | no_price (live) | air: no_price; blank: question → no_price late | split | scope (G1): `agent.handle` decides scope after `read_request`, whose question has already gone out (a blank model reading) |
| `import-implicit` | direction |  | no_price (text) | quote | wrong | scope (G4): no import said, `served_airports` turns PVG-FRA round to FRA-PVG and quotes it |
| `cross-trade` | direction |  | decline (text) | no_price late | late | scope (G5): neither end is a departure airport of ours, no rule declines; the price is sought until `hard_at` |
| `domestic` | direction |  | no_price (guess) | no_price late | late | scope (G5): neither end is a departure airport of ours, no rule declines; the price is sought until `hard_at` |
| `customs-only` | service |  | decline (text) | question → quote | wrong | scope (G6): no reading of what is asked; a service without the flight becomes an air request |
| `warehousing-only` | service |  | decline (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G6): no reading of what is asked; a service without the flight becomes an air request |
| `insurance-only` | service |  | decline (text) | question → quote | wrong | scope (G6): no reading of what is asked; a service without the flight becomes an air request |
| `truck-to-airport-only` | service |  | no_price (guess) | air: question → quote; blank: question → no_price late | wrong | scope (G6): no reading of what is asked; a service without the flight becomes an air request |
| `packing-only` | service |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G6): no reading of what is asked; a service without the flight becomes an air request |
| `complaint-damage` | none |  | no_price (text) | question → quote | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `invoice-query` | none |  | no_price (text) | question → quote | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `supplier-advert` | none |  | no_price (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `job-application` | none |  | no_price (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `newsletter` | none |  | no_price (text) | question → quote | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `out-of-office` | none |  | no_price (text) | question → quote | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `misdirected` | none |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `general-rate-card` | none |  | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `bank-details-change` | none |  | no_price (text) | air: question → no_price late; blank: question → no_price late; routed other: none | split | scope (G7): router rule `new_thread` makes every new thread a request; a mail that asks for nothing gets a question or a price |
| `air-or-sea-cheaper` | partial |  | quote (guess) | quote | right |  |
| `instead-of-sea-air` | partial |  | quote (text) | quote | right |  |
| `two-shipments-one-sea` | partial |  | quote (guess) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `air-plus-customs-storage` | partial |  | quote (text) | quote | right |  |
| `sea-or-air-german` | partial |  | quote (guess) | quote | right |  |
| `company-seaside` | lookalike |  | quote (text) | quote | right |  |
| `street-seehafen` | lookalike |  | quote (text) | quote | right |  |
| `shipping-loosely` | lookalike |  | quote (text) | quote | right |  |
| `container-loosely` | lookalike |  | quote (text) | quote | right |  |
| `port-cities-no-codes` | lookalike |  | quote (text) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `fob-air` | lookalike |  | quote (text) | quote | right |  |
| `cif-air` | lookalike |  | quote (text) | quote | right |  |
| `sea-mentioned-past` | lookalike |  | quote (text) | quote | right |  |
| `seefracht-zu-langsam` | lookalike |  | quote (text) | quote | right |  |
| `import-buyer-side` | lookalike |  | quote (live) | quote | right |  |
| `seaport-code-beside-airport` | lookalike |  | quote (guess) | quote | right |  |
| `ocean-in-product` | lookalike |  | quote (text) | quote | right |  |
| `road-freight-in-goods` | lookalike |  | quote (text) | no_price | wrong | scope (G10): `orchestrator.NOT_AIR` matches a mode said in passing, no air word in the mail stops it ('road freight' names the goods) |
| `rail-in-goods` | lookalike |  | quote (text) | no_price | wrong | scope (G10): `orchestrator.NOT_AIR` matches a mode said in passing, no air word in the mail stops it ('rail freight' in the goods name) |
| `reederei-sender` | lookalike |  | quote (text) | quote | right |  |
| `own-truck-to-airport` | lookalike |  | quote (text) | quote | right |  |
| `courier-delivers-to-us` | lookalike |  | quote (text) | quote | right |  |
| `express-loosely` | lookalike |  | quote (text) | quote | right |  |
| `sea-dept-signature` | lookalike |  | quote (text) | quote | right |  |
| `locode-airports` | lookalike |  | quote (guess) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `vessel-in-goods` | lookalike |  | quote (text) | quote | right |  |
| `not-by-sea` | lookalike |  | quote (text) | quote | right |  |
| `export-to-importer` | lookalike |  | quote (text) | quote | right |  |
| `no-mode-german` | lookalike |  | quote (text) | quote | right |  |
| `sea-forwarder-asks-air` | lookalike |  | quote (text) | quote | right |  |
| `by-ship-inbound` | lookalike |  | quote (text) | no_price | wrong | scope (G10): `orchestrator.NOT_AIR` matches a mode said in passing, no air word in the mail stops it ('by ship' for the inbound leg) |
| `port-of-loading` | lookalike |  | quote (text) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `by-rail-inbound` | lookalike |  | quote (text) | no_price | wrong | scope (G10): `orchestrator.NOT_AIR` matches a mode said in passing, no air word in the mail stops it ('by rail' to the airport) |
| `sea-italian` | mode | language of sea-spanish | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it |
| `sea-dutch` | mode | language of sea-spanish | no_price (guess) | air: question → quote; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it |
| `sea-chinese` | mode | script of sea-spanish | no_price (guess) | air: question → no_price late; blank: question → no_price late; routed other: none | split | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it |
| `air-italian-not-by-sea` | lookalike | language of not-by-sea | quote (text) | quote | right |  |
| `sea-in-attachment` | mode | channel of sea-fcl-locode | decline (text) | air: quote; blank: question → no_price late; routed other: none | wrong | scope (G8): the mode rule reads subject and body, not the attachment; scope (G9): the router can label the request other when the freight details are in an attachment |
| `air-in-attachment-sea-team` | lookalike | channel of sea-dept-signature | quote (text) | air: no_price; blank: question → no_price late; routed other: none | wrong | scope (G10): `orchestrator.NOT_AIR` matches a mode said in passing, no air word in the mail stops it (the request is in the attachment, the body names the sea team) |
| `later-switch-to-sea` | moment | moment of sea-switch-from-air: after our quote | quote → no_price (guess) | quote → quote | wrong | after the quote: a later mail's change of mode is read as a requote for air (one line to that goal) |
| `later-air-after-sea` | moment | moment of instead-of-sea-air: after our decline | no_price → quote (guess) | air: question → no_price late; blank: question → no_price late | wrong | after the quote: the session stays out of scope by its first mail, a later ask for air is declined (one line to that goal) |
| `teu-unit` | mode | unit of sea-locodes-only | no_price (guess) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it (TEU) |
| `pmc-unit` | lookalike | unit of container-loosely | quote (text) | quote | right |  |
| `sea-import` | direction | two at once: import and sea | decline (text) | air: question → no_price late; blank: question → no_price late | wrong | scope (G2): no scope reading; the mode is said in words no rule knows and the extraction schema has no field for it; the lane is an import but the model's reading asks first (G1) |
| `all-lookalikes-at-once` | lookalike | two at once: company, port words, FOB, container | quote (text) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `air-chinese` | lookalike | script of no-mode-german | quote (guess) | air: quote; blank: question → no_price late | split | generalize: the rules read no lane from this shape, the model's reading decides (blank: a question, then no price) |
| `import-written-german-imperial` | direction | language and units of import-implicit | decline (text) | quote | wrong | scope (G4): no import said, `served_airports` turns PVG-FRA round to FRA-PVG and quotes it |
| `export-written-german-imperial` | direction | opposite of import-written-german-imperial | quote (text) | quote | right |  |
| `import-written-in-attachment` | direction | channel of import-implicit | decline (text) | air: quote; blank: quote; routed other: none | wrong | scope (G4): no import said, `served_airports` turns PVG-FRA round to FRA-PVG and quotes it; scope (G9): the router can label the request other when the flight details are in an attachment |
| `export-written-in-attachment` | direction | opposite of import-written-in-attachment | quote (text) | air: quote; blank: quote; routed other: none | split | scope (G9): the router can label the request other when the flight details are in an attachment |
| `export-with-import-history` | direction | moment of import-implicit: older request quoted in a new RFQ | quote (text) | quote | right |  |
| `import-lane-in-carrier-reply` | direction | party of import-implicit: airline | no_price (text) | none | right |  |

84 cases: 30 right, 2 late, 16 split, 36 wrong.


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

### Scored quote provenance

Through round 188 (event 19609), the census links 187 scored rounds to a session and 177 to the specific
archived quote whose send timestamp matches the board's answer timestamp. The eight added session links
for rounds 1–7 and 188 have exact request-receipt matches. Rounds 152 and 172 each have two local quotes.
The board's answer timestamp identifies the later one in both cases. Round 107 has no matching local
session. The other ten rounds without a matching archived quote keep their session link and the board's
verdict. This changes audit coverage only. Owner: supply for source provenance, speed for missing receipt
or quote evidence.

### Rate desk row metadata

The 22 captured control layouts still read correctly. The probes add five correct cases and ten owned gaps
across body, DOCX and XLSX. Their expected expiry, currency, EUR value and transit are given per row in
`tests/fixtures/ratedesk-probes.json`. This audit has no direct effect on accepted quote rate.

| Variant | Expected | Observed offline | Status |
| --- | --- | --- | --- |
| T3b. Transit unit in heading or cell | 36 hours yields two transit days. A days header keeps each stated day. | The hours header gives 36 days. Hours written in a cell leave the row unpriced. A labelled hours line and a days header are read correctly. | Wrong for hours in tables, `bids._desk_rows`. Owner: supply. |
| T3c. Invalid expiry on one row | Reject that row and keep the other airline's valid option | The whole answer raises a date error before the other option is read. | Wrong, `bids.parse_desk`. Owner: supply. |
| T3d. USD header and a shared conversion rate | 330.00 USD at 0.80 gives 264.00 EUR | The rate above the table is lost from the priced rows. | Wrong, `bids.parse_desk`. Owner: supply. |

### Tariff lifecycle variants

The offline sequences in `tests/fixtures/tariffs/lifecycle/` go through the router, tariff handler, store and
pricing, then compare amounts worked out from the supplied tables. Nine of 17 sequences are right. The eight
wrong sequences below each have a supply owner in the fixture. The snapshot through round 184 had 205 tariff
rows, just as in the two earlier snapshots. It adds no evidence of a real quote affected by these variants.

| Variant | Expected from the tariff | Observed offline | Status |
| --- | --- | --- | --- |
| T8a. One lane changes, old period arrives late, future period | 330.00 EUR on the amended lane, 430.00 EUR on its untouched neighbor. An expired document changes nothing. The future rate starts on its date. | Fresh lookup does all three. | Right. |
| T9a. Amendment arrives while the quote is pending | Replace 230.00 EUR with 330.00 EUR for a same-day shipment. A future-ready shipment uses its 430.00 EUR replacement. | Reuse keeps 230.00 EUR for same-day and returns no option for future-ready. | Wrong, `session.for_request`. Owner: supply. |
| T9b. Two validity periods in one mail | 230.00 EUR today, then 430.00 EUR on the new period's first day | Correct in two attachments. In one text document only the later period survives. | Wrong for one document, `tariffs.agent.parse`. Owner: supply. |
| T9c. Correct again, then an older copy arrives | The second correction's 380.00 EUR applies. Re-sending an identical original after a correction keeps the correction's 330.00 EUR. | The DOCX correction applies. In the resend case the original 230.00 EUR regains precedence. | Wrong for resend, `store.upsert`. Owner: supply. |
| T10a. Currency or price unit changes | 330.00 USD at 0.80 yields 264.00 EUR. A 300.00 EUR rate per 100 kg still gives 330.00 EUR all-in at 100 kg. | Both amendments are priced correctly. | Right. |
| T10b. German headings in a DOCX amendment | Keep the stated dates and use 330.00 EUR | On a later mail with German date and currency headings the previous 230.00 EUR stays active. | Wrong, `tariffs.agent.parse`. Owner: supply. |
| T10c. Flat security fee of 10 or 2 EUR | At 100 kg, 330.00 or 322.00 EUR. At 200 kg, 650.00 or 642.00 EUR. | The 10 EUR sheet is rejected. The 2 EUR sheet prices 520.00 or 1,040.00 EUR because the fee is multiplied by weight. | Wrong, `tariffs.agent.parse` and surcharge basis. Owner: supply. |
| T8b. Withdraw one lane | Remove only the named lane when the withdrawal starts, and restore it on an explicit reopening | Dated withdrawal and reopening work in the supported booking notice. A differently worded immediate rate withdrawal leaves 230.00 EUR available. | Wrong for immediate rate withdrawal, `router.rules` and tariff availability. Owner: supply. |

The fixture changes timing, the old versus new source, language, document format, currency and units. It exercises
reuse while a quote is pending. Amendments received after the quote or booking, another script, and the same
freshness rule across the rate desk and direct carrier offers remain open. Owner: supply. This audit has no direct
effect on the accepted quote rate.

### Bid deadlines on quotes already sent

The snapshot through event 19799 and scored round 190 links 179 scored rounds to an exact quote timestamp.
For the deadline check, match the air bid or a unique pickup bid from each quote to the carrier's own text.
Keep only a single explicit UTC deadline in that text. On the head of [#496](https://github.com/lucakammerer/pixel/pull/496),
the parser matches all 67 source deadlines (3 air and 64 pickup). None had elapsed when the quote went out.
The shortest remaining validity was 789 seconds for air and 737 seconds for pickup. The other 54 matched
bid references have no single deadline in this UTC body format. Four have attachment metadata, which
cannot establish the deadline from the snapshot.

For 58 of the 64 dated pickup offers, the air option also has a deadline from the source mail or the stored
option. The pickup deadline is earlier in 55 of the 58. A combined quote's latest verified buying time is
the earlier of its two deadlines. Of 44 dated pickups with a linked booking record, one had already
expired when that record was created. In round 55 the pickup deadline was 20:43 UTC and the booking record
was created at 21:09 UTC. That record does not establish whether the trucker still honored the price.
Final selection at or after expiry, the combined validity shown in the quote, and first use at booking
remain open. Owner: supply for price validity, after the quote for the booking flow.

### Bid validity variants

The next snapshot, through event 19080 and round 181, adds four inbound census rows, three portal events and two
sent quotes. It adds no tariff versions and shows no further candidate for a newer sheet or later bid before
send. All 158 stored start-kit lanes match the offline source reading for dates, rate breaks, currency, transit,
minimum and surcharges. The 20 hand-worked kit prices agree. The two emailed tariff files still need their
original attachments. Owner: supply.

| Variant | Hand expectation | Observed offline | Status |
| --- | --- | --- | --- |
| T1a. UTC deadline, airline body, trucker body, airline DOCX | Keep 17:00 UTC with the stated total and transit or pickup | All three keep the amount and lead time, and drop the deadline | Wrong. Owner: supply. |
| T1b. One hour and 45 minutes from a 16:00 UTC offer | Keep 17:00 and 16:45 UTC respectively | Both keep the amount and transit, and drop the deadline | Wrong. Owner: supply. |
| T1c. German timestamp with offset, Arabic duration, French duration in a trucker's DOCX | Normalize to 17:00, 17:00 and 16:30 UTC | All three keep the amount and lead time, and drop the deadline | Wrong. Owner: supply. |
| T1d. Departure time or an old expiry quoted below the new offer | Keep the offer without an expiry | Both keep the new total without an expiry | Right. |
| T3a. Desk offer in 22 attachment layouts | Keep the per-airline amount, currency, transit, rate ID and 14:38 UTC expiry | All 22 preserve those fields with the RFQ supplied | Right for reading. Thread correlation and expiry before reuse remain open. Owner: supply. |
| T7a. Missing portal dataset version | Carry the source's dataset version when it supplies one | Four options in round 100 lack it, including the one used in an accepted quote. The raw response is unavailable. | Unknown whether the source supplied the version. Owner: supply. |

The validity probes run through the real bid recorder with no gateway, using `rfqs/bid-validity.json`. These
findings describe the baseline before validity was retained. The reader now passes all 14 probes. The four new
probes cover an ISO timestamp with an offset, conflicting deadlines, a missing timezone, and a trucker's validity
duration with no pickup time. The desk still passes all 22 layouts. Among 65 selected bids with an explicit UTC
expiry in the body, none was expired at send. For 153 of 154 portal quotes in the next snapshot, the option holds
the most recently observed version on its lane, carrier and billed weight. Round 100 has no recorded version.

Open for T1 and T2: validity only in a separate attachment beside a price in the body, or in a scan the rules
cannot read. Open for T11: expiry before use, a later correction, and the same cases after a booking. The
individual pickup bid now keeps its stated expiry. The quote's combined air and pickup validity still needs
the earlier of the two. Owner: supply. The change has no expected effect on offer totals or accepted quote rate.

Steps already covered by this census include air versus truck versus desk, body versus attachment, reply linked
to an enquiry versus a separate desk thread, and source received before the quote versus a fresh rate read for
booking. The open variations still need opposite cases, pickup and air, arrival just before the quote or after
it or after booking, body and file and later correction, another language and script, another unit, and a
combined change. Extend the existing bid, sheet and desk checks in `tools.benchmark_generalization` and
`tools.simulate`. The census and this table have no direct effect on the accepted quote rate.

## Axis P: price against the scorer's best (22.11.2026 13:20, live database to round 190)

`uv run python -m tools.price_ledger <database>` prints the rows below from a copy of the live database: every
round lost on price or on accuracy points, and every accepted round today's code would quote otherwise (none today:
round 46 gives 1731.19 against 1731.20). Today = current `main` on the options the session had when its quote was
drafted, plus what today's code adds that the session lacked (rate card on an outage answer, pickup memory, the
insurance premium, today's reading by rules). A rejected round names no best price, only its side: `too_low` puts
it above quoted / 0.95, `too_high` below quoted / 1.05. "Past the bound" means today's total lies beyond that bound,
so it is within 5 % of the scorer's best for every best up to the range shown; it is not proof of the best itself.

**Headline: 28 rounds were lost on price, not 68** (18 `too_low`, 10 `too_high`; the 68 counted `feedback` events:
round 93 was written 40 times, once per 10 s poll from 00:21 to 00:28 UTC, round 94 three times). **Today 26 of the 28 move past the verdict's
bound with the source, rule or reading that was missing then. 15 and 16 pass once the customer names the pickup place,
which today's code asks for (proven on 17, 18, 20, 131 and 189: asked, Nordlicht named Cologne, quoted CGN zone 50, 45 of 45); the place they would name is not in the database.**
Cycle 4 added 130 (the document's 1144 kg, proven by the cross-check's own record) and 67 (Domblick's voice message,
read since 21:59, priced from its other answers). Cycle 2 added
17, 18 and 20 (priced at the place the customer named when asked) and 33 to 35 (priced at the figure today's rules
read from the fixture of the round's attachment). Of the three accepted rounds that lost accuracy points with a session in the database, all three give the
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
| 67 | too_low | 3345.48 Riverbend + Rheinufer 45.00 | 3659.12 Riverbend + Domblick 358.64 / 4 h (Domblick's price from its five other zone-40 answers, 214.60 at 620 kg to 379.97 at 1892 kg, all 4 h) | 3521.56 to 3851.71 | option never had: Domblick sent its offer only as a voice message (mp3), never read; with the truckers we had nothing reaches the bound (Hansekogge 220.24 gives 3520.72). Also "45.00 EUR for each hour" read as Rheinufer's price | audio read since 21:59 UTC (`bids._with_attachments`, `tariffs.agent.attachment_lines`): the four trucker voice messages after it were priced by rules; `selection.implausible` drops the misread |
| 179 | too_high | 1410.83 Meridian 1 d + Westfalenroute | 1074.69 Aurelia 2 d + Westfalenroute | 1023.51 to 1131.25 | option had, not selected: `calc.arrival` added a day for the trucker; the scorer counts ready + transit | hot repair A, on `main` (`calc.arrival`) |
| 130 | too_high | 3753.00 Aurelia at 1510 kg + Grautal | 2936.82 Aurelia 2 d at 1144 kg + Grautal | 2796.97 to 3091.39 | reading: the rules read 1144 kg from the packing list (`rfq.evidence`: weight from `packing-spec_65140.pdf`), the cross-check replaced it with the mail's 1510 (`differs` holds [rules, model] = [1144, 1510]) | 2ef42c6: a figure the rules took from a document is never replaced by the cross-check (`extract._crosscheck`, `from_documents`); priced offline at 1144 kg |
| 33 | too_high | 1475.00 Borealis at 2.78 cbm | 1083.76 Borealis at 1.92 cbm (320.64 kg) | 1032.15 to 1140.80 | reading: document 1.92 cbm against the mail's 2.78 | #159: today's rules read 1.92 from the SLI (`test_extract`, `rfq-sli-volume-differs-from-mail`); priced offline at it |
| 34 | too_high | 4980.00 Solace at 11.69 cbm | 3944.40 Solace 2 d at 7.49 cbm (1384 kg) | 3756.57 to 4152.00 | reading: document 7.49 cbm against the mail's 11.69 | #159: today's rules read 7.49 from the packing list (`test_extract`, `rfq-packing-list-volume-differs-from-mail`); priced offline at it |
| 35 | too_high | 8751.64 Nova Atlas 3 d at 2644 kg | 6620.00 Nova Atlas 3 d at 1751 kg (billed 2000) | 6304.76 to 6968.42 | reading: the spec's 1751 kg against the mail's 2644 | today's rules read 1751 from the xlsx (`test_extract`, `rfq-spec-weight-differs-from-mail`); priced offline at it |
| 20 | too_high | 3723.64 Quetzal FRA 2 d + Bodenseefracht at 61391 (guessed) | 3404.90 Juniper MUC 1 d + Neckarstrom at zone 86 | 3242.76 to 3546.32 | reading: "from our warehouse", no place; signature Nuremberg (zone 90, not listed) | today `extract._ask` asks for the postcode; Weidner named Augsburg when asked (`_town_zone`), priced offline at MUC zone 86. A question for the pickup place is accepted (round 131: Auer named Augsburg, quoted MUC, 45 of 45) |
| 16 | too_high | 1688.10 Helix 2 d + Bodenseefracht at 36627 (guessed) | not comparable | — | reading: "our warehouse", no place | today asked (as 131); Nordlicht was never asked, its place is unknown; generalize |
| 15 | too_low | 1058.20 Polaris MUC, no pickup | 1258.85 Polaris MUC + Neckarstrom at zone 93 (Regensburg), or 1725.08 Juniper HAM + Hansekogge at zone 24 | 1198.90 to 1325.11, or 1642.93 to 1815.87 | reading: "our warehouse", no place: priced without pickup | today asked (as 131); Auer was never asked; either of its two likely places passes; generalize |
| 17 | too_low | 3844.65 Wayzata HAM, no pickup | 4255.50 Wayzata CGN 4 d + Westfalenroute at zone 51 | 4052.86 to 4479.47 | reading: "our warehouse", place guessed (21663) | today asked; Lindqvist named 51115 Bergisch Gladbach when asked (round 18), priced offline at CGN zone 51 |
| 18 | too_high | 2602.88 Juniper HAM + Maintal at 21663 (guessed) | 2358.88 Lumen CGN 4 d + Westfalenroute at zone 51 | 2246.55 to 2478.93 | reading: "our warehouse", place guessed | today asked; the customer wrote 51115 Bergisch Gladbach after our quote, priced offline at CGN zone 51 |
| 27 | accepted, 39.32 | 3018.37 Meridian bid 2877.65 + Hansekogge | 2980.72 Meridian card 2840.00 + Hansekogge | passes, best 2980.75 from the points | option never had: the portal's price during its 503, the airline's bid by mail 1.3 % above it | rate card |
| 13 | accepted, 33.0 | 2875.06 Borealis + Alpenblick | 2953.82 Borealis + Hansekogge | passes, best 2953.82 | trucker rule | `selection.balanced` (`times_the_best`) |
| 136 | accepted, 44.9 | 467.64 Borealis at 0.46014876 cbm | 467.53 at 0.46 cbm by hand (339.65 / 76.845 x 76.82 + 127.99) | passes, best 467.54 from the points | reading: a converted volume not rounded to 0.01 | 66b462b (`extract.CONVERTED`) |

Rounds 1, 2 and 4 lost accuracy points too; their sessions are not in this database.

How far the two sources behind 13 of the 18 can be trusted, on the same database:

- Rate card (`uv run python -m tools.card_proof <database>`): of 190 portal rates it could price from the reads
  before them, 133 read in the same version within 0.001 %, 56 scaled from another version within 0.4 %; the one
  outlier (11.8 %) was the first read of a new version, which no card can know.
- Pickup memory (`uv run python -m tools.memory_proof <database>`): of 110 door requests with two or more timed
  trucker answers, the memory of the answers before each priced 78: the same balanced trucker in 75, median
  difference 0.10 EUR, none more than 5 % of the all-in (14 above 1 %, 5 above 2.5 %; worst 4.2 %, round 17's zone
  21, where Odenwald's first answer was cheaper than every earlier one).

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
| A figure the attached document states over the mail's (weight, volume) | 3 accepted since (`test_extract`) and 33, 34, 35 at the document's figure | 0 | 130 too, by hand at 1144 kg |
| A pickup without a place is priced at the place the customer names when asked | 2 (131: Augsburg; 189: Cologne; 45 of 45 each) and 17, 18, 20 at the named place | 0 | 15 and 16: never asked |
| A converted unit rounded to 0.01 before pricing | 1 (136, by the points) | 0 | — |
| An airline bid or the rate desk as the price | 4 (29, 45, 46, 47) | 0 | — |
| Minimum on the freight, surcharges on billed weight, a quote in another currency | no scored round tests them | — | every sheet quoted above its minimum; no sheet quoted at a higher break with surcharges; every request asked EUR |

Steps sideways from round 179 (arrival from ready + transit): a weekend in transit, a ready date in the past, a
pickup longer than a day, the portal's transit against the sheet's, "by Friday". Only 170, 118 and 179 carry an
arrival limit, so none of these is tested by a scored round. Cheapest live evidence: the next round with an arrival
limit and a door pickup whose ready day is a Friday, read with `tools.price_ledger`.

Every scored round, cycle 5 (the ledger's last line, live database to round 190): 190 scored, 140 accepted. Of
these, today's code quotes 127 exactly as sent and 1 a cent apart (46); all 131 with a session, the 3 that lost
accuracy points included (13, 27, 136), are within 5 % of the best their points give (at 45 points: the quote
itself); 9 have no session in this database (1 to 7, 107, and one not yet matched). 28 lost on price, above. The
other rejections were judged on something else than the price (Axis R). 186 (`not_what_was_asked`, 22.11. 09:45:
a Turkish "en hızlı seçeneği istiyoruz", fastest, read as cheapest) passes on today's `main`: hot repair A's reading
gives fastest, so Fjordline 2 d at 1194.71 instead of Aurelia 3 d at 844.49.

Open, none of them a price or selection rule: 15 and 16, the place the customer would name (generalize). Predicted,
not yet live: one trucker's answer unreadable (a scan, a failed transcription) while the others priced; the pickup
memory then prices only the others, as in 67. A fix (`pickup.estimates` for every trucker that answered without a
price read) has one scored round behind it and waits behind the one-pull-request rule. Rounds 180 to 184 (cycle 2, fresh copy): all accepted, today's code quotes them as sent. Nothing in this axis is wrong today that has no owner.

## Axis F: after the quote (22.11.2026, snapshot through round 192)

The snapshot ends at event 19995 (10:28:26 UTC). Inspect the recorded phase, sender, routing, decisions and
linked outgoing replies with `uv run python -m tools.followup_matrix --db <copy>`. Use `--after-event 19893`
for the nine added mails since the previous snapshot. The fixture run of the same tool checks today's rules and
selected tracking outcomes. `tests/test_booking.py` checks the booking actions with mocked services.

### What the scorer scores

192 rounds were closed in this snapshot.

| Evidence on the closed rounds | Observed verdicts and points | Interpretation |
| --- | --- | --- |
| Customer reply recorded in the RFQ session: 188 rounds vs 4 without | 141 accepted, 45 rejected and 2 no answer vs 4 no answer | Descriptive. A reply often follows the verdict-driving quote. |
| Airline confirmation linked to the RFQ: 118 rounds vs 74 without | 115 accepted and 3 rejected vs 26 accepted, 42 rejected and 6 no answer | The confirmation event and the scorer's `booked` are different observations. |
| Tracking answer linked to the shipment: 113 rounds vs 79 without | 113 accepted, 108 with +10, vs 28 accepted and none with +10 | Every +10 coincided with `booked: true`. Seven `booked: true` rounds scored zero. |
| Booking field on the board | `booked: true` in 115, false in 6 and null in 71. All 121 `points.booking` entries are 0. | What earns a separate booking bonus remains open. Check the next scored booking. |
| Price-match requests in rounds 108, 132, 144, 157, 166 and 175 | 144 and 166 show `booked: false` and adjustment +10. Both were booked before the unconditional yes. The four without a reply had neither outcome. | Hold the quote and wait for the yes. The next clean hold can isolate what earns the adjustment. |
| Withdrawal after booking (round 88) | Adjustment −20 when the cancellation was missed | Fixed in #336. |
| Change after the quote (rounds 86, 152) | 86 counted the update as unanswered. 152 scored the quote sent in reply to the change. | A changed request can have a new scored quote. |
| Questions about transit, inclusions or validity, or a second RFQ in the thread | No scored example found | Check the first live example of each. |

Rounds 180–185 add five bookings, each with tracking +10, and one message promising dangerous-goods paperwork.
Round 187 also promised paperwork. The quote was accepted by the scorer in both paperwork rounds. The decline in
round 186 arrived after the quote had already selected the wrong service. Its reading error affects the CRM state.
The first-quote loss belongs to the selection repair in #508.

Round 188 closed with tracking 0.0. The answer matched its confirmed shipment after a Voice update to `at_origin`,
printed the internal status code and omitted the airline booking reference and carrier. Rounds 189–191 matched confirmed
shipments and scored +10 each. The scorer's specific reason for 188 is open. The next question about an intermediate status
can help isolate which missing fact mattered.

### The census

1,846 incoming replies inside sessions, plus 17 standalone airline confirmations and 114 standalone tracking desk
questions. The recorded phase is taken before `mail.in`. A later `followup.read` gives the phase when the handler ran.
Standalone confirmations and tracking questions use the linked shipment session's phase, or unknown if the log
has no link. Tracking is linked using `tracking.answered.shipment_sessions`.

| Sender and kind | Count | Action recorded |
| --- | --- | --- |
| Airline or trucker, bid | 1,289 | 1,248 arrived while the session was still open, 41 after the quote or acceptance. 1,283 have `bid.received`. Six earliest or manual-session replies have processing errors, owner Supply. |
| Airline, confirmation | 126 | 118 confirmed, six duplicates, two unmatched on historically stuck mail bookings (rounds 27 and 29, #323). The confirmation in round 171 hit a mailbox error when telling the customer (C15, Generalize). |
| Customer, reply | 435 | 125 acceptance and 38 decline events, plus corrections, cancellation, paperwork and acknowledgments. This includes three price-match requests logged as declines and two as acceptances. |
| Customer, answer to our question | 12 | Routed as `other/thread:question`. Cases and first-quote outcomes are handled in Scope and the request-scoring axes. |
| Trucker, attached pickup offer | 1 | Routed as a tariff and failed (round 69, T4, Supply). |
| Tracking desk, question | 114 | Latest recorded answer matched 110 shipments at `confirmed`, one at `at_origin` (F23), and two stuck in `booking`. One question remains unmatched. Three were unmatched at some point, including two later matched. |

The historical `reply_unhandled` cohort was 119 notes across 67 sessions before 22.11. The mail bodies show 107
acknowledgments, six promises of dangerous-goods documents, two pickup answers, two corrections and two cancellation
messages (one request and one follow-up). Later notes include two sender-change attempts outside the original session
(Scope), a price-match request (F1) and two no-price acknowledgments (F17). Current rules suppress the ordinary
acknowledgments. An existing signature-reading gap accounts for one of the no-price acknowledgments.

### The table: every case, right action, today

Final-window priority is F1 (six scored rounds). The #480 fix shown in F1 was reviewed and closed without a
merge. Its replacement is on local branch `after-quote/price-match-2`. #498 covers F6–7 and #505 covers F4–5.
F20 was reproduced offline. The snapshot has no observed customer request for a different customer's shipment.

Steps taken from each live case: the opposite (a competitor mentioned in a decline; a cap in "match 4,000 EUR"),
another party (airline, trucker, rate desk, tracking desk), another moment (open, quoted, declined, accepted,
booked, confirmed, cancelled), another channel (body, attachment, a new thread quoting ours), another language
(en, de, fr, es, it), another unit (kg, lb, tonnes, litres), and combined cases (a yes and a match request, or a
change and a yes). Tracking covers the customer and the desk, own and unknown references, and a foreign reference
in the body or attached to a French question. The milestone cases compare the recorded Voice statuses with
a normal confirmation, including the English draft for a French attachment enquiry.

| # | Case | Live | Right action | Today | Status, owner |
| --- | --- | --- | --- | --- | --- |
| F1 | price-match request after the quote, en/de/fr, after a decline | 6 rounds | `hold`: one reply keeping the price, quote open, the yes books | `hold` | fixed #480 (review) |
| F2 | price-match request that declines in its own sentence | never | decline | decline | right |
| F1b | an acceptance conditioned on a stated lower price ("we accept the quote provided you match 2,900 EUR") | fixture on local branch | book when our total meets the cap; otherwise keep the quote open | an acceptance can book without the cap check | **gap**, after the quote (`followup.conditional`) |
| F3 | price-match request after the booking | never | person | person | right |
| F4 | acceptance in Spanish or Italian | never | book | nothing | **gap**, after the quote (`booking.ACCEPT`) |
| F5 | decline "nous ne donnerons pas suite" | never | decline | nothing (quote stays open) | **gap**, after the quote (`booking.DECLINE`) |
| F6 | weight corrected "1,952 kg, not 753 kg, please correct the offer" | once after the booking (21.11.) | requote at 1,952 kg | person, no requote | **gap**, after the quote (`followup.changes`, `UPDATE`) |
| F7 | the same correction after the confirmation | 21.11. 15:51 | person with the change | person, change unread | **gap**, after the quote |
| F8 | ready date moved after the booking | never | person | nothing | **gap**, after the quote (`followup.changes`) |
| F9 | acceptance in a new thread quoting our quote | never | the old session books | new request, quoted again | **gap**, Speed (`router.rules`) |
| F10 | booking confirmation for an unknown reference from an unlisted address | never | confirmation, unmatched | new request | gap, Speed (B13) |
| F11 | question about the quote (transit, inclusions, validity), documents after the booking, a reminder | never | unknown: a person today; a reply from the quote's own facts if the scorer scores it | person | open, after the quote; scorer unknown |
| F12 | second RFQ in the thread | fixture | scoring unknown under R4 | requote after the quote, person after booking | open, after the quote (B15) |
| F13 | cancellation before our quote ("please disregard my request") | never | unknown: R1 says always answer | person, the quote still goes out | open, scope |
| F14 | acceptance after the rate expired | test | book a fresh rate | book (`portal_rates.refresh`) | right (B1) |
| F15 | tracking question before any booking | never | person | person | right |
| F16 | the same mail twice | test | one action | one booking (`test_acceptance_books_once_and_tells_customer`) | right |
| F17 | no-price acknowledgment with a bare sender name | one current rules miss, mail 48194 | nothing | `human.note` | **gap**, after the quote (`_acknowledges_no_price`) |
| F18 | airline refuses or changes a booking | never | the next option, or a person now | person (B7) | open, Shared (`booking.handle_confirmation`) |
| F19 | booking failed, customer silent | never | a notice or a person within the round | nothing to the customer (B9) | open, Shared |
| F20 | customer tracking asks for another customer's reference, or names an unknown reference in their own thread | fixture | no shipment details; ask for a valid reference | returns the other shipment or falls back to the thread's shipment | **gap**, after the quote (`tracking.handle`) |
| F21 | a refusal framed as "I'll have to pass" | round 186, mail 56284 | close quote as declined | `thanks`, quote stays open | **gap**, after the quote (`booking.DECLINE`) |
| F22 | cargo correction in a CSV with pieces, weight and volume only | generated fixtures | read the corrected cargo from the attachment | figures unread | **gap**, Accuracy (`extract._table` expects dimensions) |
| F23 | tracking after a Voice milestone update | round 188 plus the other Voice statuses as fixtures | state the milestone in plain language, with the carrier and both references | airline and airline reference omitted; round 188 scored tracking 0.0 | **gap**, Hot Repair for the live loss; after the quote for milestone variants (`tracking._paragraph`) |
| F24 | objection to the quoted option without a new request or explicit refusal | round 192, mail 57922 | send to an operator | `thanks`, no action | **gap**, after the quote (`followup.read`); scorer rejected the first quote for `not_what_was_asked` |

0 wrong outside these known gaps (`tools.followup_matrix`: every non-right case carries its `known_gap` and owner).
Next in this lane, most frequent and cheapest first: F4 and F5 (one regex each), F6 and F7, F8, F17.
