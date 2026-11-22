# Generalization fixtures

Reference data for checking behaviour across formats and wording, written by hand and not generated from the code
under review. The captured mails and their descriptions are in `mails/README.md`; this file covers the rest.

- `tariffs/hand_prices.csv`: twenty worked prices, two per start-kit tariff, each with the selected break, billed
  weight, freight after the minimum, both surcharges and the rounded total.
- `tariffs/source_review.csv`: every start-kit source file with its lane count and format risks. What was compared:
  each file's lane count and `check()` findings, plus the sample lanes in `tariffs/expected.json`.
- `tariffs/lifecycle/`: synthetic carrier mails and calculated spot prices before and after lane updates, withdrawals,
  currency and unit changes, corrections and resends. Benchmark section 5 routes and ingests each mail, then reads
  the rates directly or reuses the options a pending quote already holds.
- `tariffs/variants/`: synthetic sheets in the kit's text layout with one change each (breaks, currency, decimal
  commas, dates, transit wording, column order, surcharge layout), written by `make_variants.py`, which computes the
  expected prices without `pixel/pricing/calc.py`. `variants.jsonl` says which outcomes are safe: parsed right, or
  refused so the model reads the sheet. Parsed into wrong values is never safe.
- `rfqs/rewrites.jsonl`: several requests (lanes, weights, volumes, dimensions, fastest, door pickup), each written
  many ways, languages and number formats, with the same expected fields. `probe_*` rows test phrasings that trip
  keyword rules.
- `lane-declines.json`: an airline's decline of our lane with the lanes it flies, in the captured shape and written
  ones (list first, bullets, lower case, separators), plus declines that must teach nothing: a list that names the
  declined lane, says it is incomplete or is cut short, a decline for capacity, a decline without a list.
- `ratedesk-layouts.json`: the rate desk's captured answer as tables (header names, column order, currency in the
  header or the cell, German), with each priced airline read by hand; benchmark section 16 writes its own lines and
  each table into every attachment format behind a mail that only points to the file. It compares currency and
  expiry as well as total, transit and rate ID.
- `ratedesk-probes.json`: synthetic desk replies in body, DOCX and XLSX with transit hours in a heading or a cell,
  shared FX and a malformed date beside a valid airline row. The benchmark compares the hand-read rows and reports
  known gaps with their owner.
- `rfqs/bid-validity.json`: carrier offers with expiry in the body or an attachment, measured from the simulator's
  16:00 UTC receipt time, with expected timestamps written by hand. The departure date and quoted old offer are
  controls. `tools.simulate` runs each through `bids.record` offline.
- `rfqs/followup-answers.json`: answers to our one question, each naming another airport beside the request's;
  benchmark section 15 appends each to every captured request and requires the same fields. `known_gaps`:
  `"<answer>:<fixture>"` read wrong today, each ending `Owner: <role>.`
- `sessions/<name>.json` and `<name>.state.json`: a live session written by `tools/capture.py`. The first holds its
  incoming mails as the API returned them (`tools.replay` replays it); the second the rows it met on arrival
  (tariffs, carriers, zones, the rate card of its lane, our mails of the session, the customer's earlier requests,
  the truckers' answers of the last 24 hours) and the portal's rates and weight breaks as the API gave them, which the replay serves
  again (a reading rounded since gets the rate times its weight), and under `answers` what the model returned live
  by prompt hash (recorded since 22.11.): the same prompt gets that answer, free; any other model call fails
  offline. The question's archived receipt lets the replay ask again and link the reply to the question it just
  sent, or to the original request if it has enough facts. Attachment bytes are not stored. `round-166-accepted`
  is the session `tests/test_capture.py` replays end to end (portal rate, its next weight break, trucker bids) and
  `tests/test_outage_drill.py` replays under every drill. `round-56-version-rollover` saves the arrival state,
  the scorer's best and the first recorded rates of the new version in `.expected.json`. It exercises the
  portal outage both before and after those reads are available to the rate card (C16).
- `replay-gate.json`: round 155's session as the live database held it (request mail, reading, every option found,
  the sent quote, pickup zone), which `tests/test_replay_gate.py` builds into a database for `tools/replay_gate.py`.
- `thread-followups.json`: round 86's session with every mail that can arrive later in it, by sender, phase and
  intent, and templates for the change mail in every language, unit system and attachment format;
  `tools/followup_matrix.py` runs it (benchmark section 17).
- `rfqs/partial_cases.jsonl`: incomplete requests that still have a price basis, and ambiguous or contradictory
  ones, with the facts that may be inferred and those that must stay unknown.
- `voice/`: two captured mails whose content is only a voice message (mp3 embedded, 200 KB and 218 KB): `rfq-voice-message.json`
  (round 65, every fact of the request spoken) and `bid-pickup-voice-message.json` (round 67, a trucker's
  pickup offer spoken). Outside `mails/`, so `tools.kpi` and the rules' corpus do not run the local speech model
  on every gate; `uv run python -m tools.replay tests/fixtures/voice/<file>` replays one with its transcript.
- `rfqs/live.jsonl`: one row per captured customer request (`mails/rfq-*.json`) with the fields a person read
  from the mail itself: lane, gross weight, volume, pieces, wish, pickup and its postcode, currency, language. A
  field left out is not judged (the origin of a door-to-port request comes from the pickup zone, not the mail);
  `null` means the mail does not state it. The benchmark scores the rules on every row per field, and on the same
  mails changed as seen live (an old request quoted below, a reply's subject naming another lane, pounds), whose
  answers follow from the row; the old request and the reply subject they add, as seen live, are in
  `rfqs/live-mutations.json`. A new captured request: store it with `tools.replay --capture`, then
  `tools.benchmark_generalization --stub` prints its row with every field `"?"`; read each from the mail, never
  copy what the code read, or the row repeats today's misread. A missing row or a `"?"` is printed as a to-do,
  never failed, so a capture cannot stop the deploy job. A field the rules misread today carries `known_gap` on the
  row, a change they misread `known_gap_mutations` (`{"pounds": "<note> Owner: <role>."}`); both are reported as
  gaps instead of failures.
- `mails/routing_probes.jsonl`: mails as the API delivers them, each with a setup (a reply to our quote or bid
  request, a sender on the carrier list) and the kind a right router gives.
- `mails/bid_probes.jsonl`: carrier answers with the total, currency and transit a right reader takes from them.
  Refusing is always safe: the model reads the answer then.
- `pickup-offer-layouts.json`: trucker offers sent only as an attachment (letter, table, quotation), with the
  attachment's text as `tariffs.agent._lines` reads it, the price and the pickup hours a right reader takes. The
  benchmark's `PICKUP_CHANGES` changes each one way (number style, currency notation, total label, waiting charge,
  lead time) and requires the same figures or a refusal.
- Spreadsheet changes need no files: `tools/benchmark_generalization.py` `XLSX_CHANGES` edits every kit `.xlsx` in
  memory and requires the original's tariffs or a refusal. Known gaps for them go in
  `tariffs/variants/xlsx_known_gaps.json`, keyed `change:file`.
- `rfqs/simulate-sources.json`: for every way `tools/simulate.py` relates a mail to its attachment (agrees,
  contradicts, silent, an explicit correction in four wordings, another shipment's document, a document that
  contradicts itself), which source a right reading takes for weight and volume, and the round or pull request that
  says so. The generator computes each case's answer from its own figures and this table, never from the code.
  `rfqs/simulate-known-gaps.json`: the classes read wrong today, by axis values, each ending `Owner: <role>.`, and
  for carrier answers (`bid`) also the classes left to the model, which cost a gateway call per bid;
  `tests/test_generalization.py` fails on a wrong case outside them and on a gap that reads right. The same
  family puts each other kind's decisive fact (an acceptance or decline, the tracking desk's reference, a booking
  confirmation's references, a whole tariff) only in an attachment, in every format.
- `channel-gaps.json`: the cells of the channel matrix (`tools/benchmark_generalization.py` section 12: every mail
  kind against where its information sits, body, pdf, scan, docx, xlsx, txt or csv, audio, or body and attachment
  disagreeing) that no captured mail, kit sheet, pickup offer or generated case covers yet, keyed
  `"<kind> <channel>"`, each note ending `Owner: <role>.`. A cell with neither fails the benchmark and the tests, and
  so does a listed cell that a case covers now: its note goes in the same commit.
- `next-cases/` and `next-cases.json`: written requests for the new kinds of requirement we expect next
  (`bench/NEXT_CASES.md`), each with a hand-worked expected answer, today's offline replay and a `known_gap` while it
  fails. Outside `mails/` so that neither `tools.kpi` nor the benchmark gates on them; no test reads them yet.
- `bid-plausibility.json`: every bid with peers of its kind from the scored rounds of 21.11.2026, read right, and
  round 67's misread trucker bid with the quote a right reading gives (`selection.implausible`).
- `tracking-questions.json`: the live CRM's booked shipments of 21.11. and the 44 tracking questions the desk sent
  about one of them, each with the shipment a person matches from the desk's facts. The benchmark's
  `TRACKING_CHANGES` changes each question one way (weight notation, lane separator, company form); it must find the
  same shipment or ask for the reference, never name another.


  `rfqs/simulate-known-gaps.json`: the classes read wrong today, by axis values, each ending `Owner: <role>.`;
  `tests/test_generalization.py` fails on a wrong case outside them and on a gap that reads right.
- `scored-rounds.json`: every scored round of 21.11. with a known best price (32; rounds 11 and 14 are left out:
  their pickup bids were stored for an origin read before the one we quoted), each with the request's pricing
  fields, every option found before our quote and the scorer's best total, airline and trucker from
  `GET /v1/board/me`. `tests/test_scored_rounds.py` requires today's pickup choice and selection to give exactly
  that, in either order of the options. Of the balanced rules only `times_the_best` fits all 32.
- `scored-airline-bids.json`: a scored round with two priced airline bids, a sheet price and the scorer's verdict:
  among airlines the customer's wish decides, not the 50/50 rule of R15.

- `unmet-asks.json`: for the last reader (`pixel/agents/rfq/unmet.py`), stored rounds with the request as the
  session held it, the reply we sent, the files it carried and the asks a right reader lists (`expected`: class
  and the customer's sentence): the first round of each new requirement we lost (rounds 10, 15, 42, 70, 76, 89,
  91, 113; round 89's reply is our question, no quote left), and accepted rounds of the same kinds (73, 80, 109,
  127), where nothing is unmet. `tools/bench_unmet.py` scores the prompt on them through the gateway;
  `tests/test_unmet.py` only checks that each expected answer is one the reader can return.

A row or variant may carry `known_gap`: the reference answer is right and the code is not yet. The note names what
the code does, why it matters for the price, and ends with `Owner: <role>.`. The benchmark reports it as a gap
instead of failing, so a fixture can land before its fix, and says when the case reads right: then delete the
note. `uv run python -m tools.benchmark_generalization --gaps` lists the open gaps by owner.

`tools/benchmark_generalization.py` prints every check; `tests/test_generalization.py` fails on anything it
reports as FAIL. Scanned sheets are read by the real scan reader with the model's answer replayed from
`tariffs/<carrier>-model-pages.json`, so both run offline and free.

The values come from the November 2026 start kit; replace or version them when the dataset changes.
