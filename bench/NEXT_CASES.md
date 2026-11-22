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
| C5 | `gateway-down-model-only` | gateway 503 / 429 / timeout for the whole request | new | requests the rules read: quoted; model-only (Chinese, voice, unclear): `quote.missed no_price` | one retry inside the call's wait; a full outage stays unquotable for these | `llm.ask_json` | S | 2×3×2 |
| C6 | `gateway-model-unavailable` | 404 for gpt-4.1 (`PIXEL_MODEL_CHECK`), gpt-4.1-mini still served | new | model-only extraction fails as in C5 | one retry on `PIXEL_MODEL` | `llm.ask_json` | S | 1×3×3 |
| C11 | `out-of-order-visibility` | 91012 visible before 91011, 91011's push lost | new | 91011 is never polled again | every Nth poll from a few ids back; `ingest` drops duplicates | `poller.run` | S | 1×3×2 |
| C14 | `large-attachment` | 9.5 MB, 400-page scan as the only document | new | unmeasured; parse unbounded, no Rfq at takeover | bounded parse; what is read by then counts | `extract._attachments` | M | 1×3×1 |
| C8 | `push-timestamp-format` | `X-Webhook-Timestamp` in ms or ISO | new | 401 / 500, the 1 s poll takes it: latency only | read like a stored time | `webhook.hook` | S | 2×1×3 |
| — | pushes stop | the webhook subscription is dropped | new | covered: the poll is the safety net (`test_poller`) | — | — | — | — |
| — | push twice / out of order | same push repeated or an older one after a newer | new | covered: stored once by id (`test_webhook`) | — | — | — | — |
| — | poll 429 with a slower minimum | "one poll per 5 seconds" | live (22.11. 01:23) | covered: interval learned from the 429 | — | — | — | — |
| — | mailbox 429 on send | 60 mails per minute (R7) | new | covered: waited out (`test_outbox`) | — | — | — | — |
| — | restart with a request open | deploy or crash in a request's second minute | live (every deploy) | covered: `orchestrator.recover`, handler kept to `hard_at` (`test_recover_restarts_an_open_request`) | — | — | — | — |
| — | ready date near midnight | "ready today" at 23:30 UTC, 00:30 CET | new | not reproduced: which day the scorer takes is unknown | — | `extract` dates, `pricing/session` validity | ? | — |

Open and unverified: C5, C6, C8, C11 and C14 have no fix. C6, C8 and C11 come from code reading, C14 is not
measured, and the midnight case is not reproduced. The live effect of #327, #331 and #333 is unmeasured until each is
merged and the organisers turn that lever.
