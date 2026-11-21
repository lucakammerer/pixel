# Generalization fixtures

Reference data for checking behaviour across formats and wording, written by hand and not generated from the code
under review. The captured mails and their descriptions are in `mails/README.md`; this file covers the rest.

- `tariffs/hand_prices.csv`: twenty worked prices, two per start-kit tariff, each with the selected break, billed
  weight, freight after the minimum, both surcharges and the rounded total.
- `tariffs/source_review.csv`: every start-kit source file with its lane count and format risks. What was compared:
  each file's lane count and `check()` findings, plus the sample lanes in `tariffs/expected.json`.
- `tariffs/variants/`: synthetic sheets in the kit's text layout with one change each (breaks, currency, decimal
  commas, dates, transit wording, column order, surcharge layout), written by `make_variants.py`, which computes the
  expected prices without `pixel/pricing/calc.py`. `variants.jsonl` says which outcomes are safe: parsed right, or
  refused so the model reads the sheet. Parsed into wrong values is never safe.
- `rfqs/rewrites.jsonl`: several requests (lanes, weights, volumes, dimensions, fastest, door pickup), each written
  many ways, languages and number formats, with the same expected fields. `probe_*` rows test phrasings that trip
  keyword rules.
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
  attachment's text as `tariffs.agent._lines` reads it, the price and the pickup hours a right reader takes.
- Spreadsheet changes need no files: `tools/benchmark_generalization.py` `XLSX_CHANGES` edits every kit `.xlsx` in
  memory and requires the original's tariffs or a refusal. Known gaps for them go in
  `tariffs/variants/xlsx_known_gaps.json`, keyed `change:file`.
- `rfqs/simulate-sources.json`: for every way `tools/simulate.py` relates a mail to its attachment (agrees,
  contradicts, silent, an explicit correction in four wordings, another shipment's document, a document that
  contradicts itself), which source a right reading takes for weight and volume, and the round or pull request that
  says so. The generator computes each case's answer from its own figures and this table, never from the code.
  `rfqs/simulate-known-gaps.json`: the classes read wrong today, by axis values, each ending `Owner: <role>.`, and
  for carrier answers (`bid`) also the classes left to the model, which cost a gateway call per bid;
  `tests/test_generalization.py` fails on a wrong case outside them and on a gap that reads right.
- `bid-plausibility.json`: every bid with peers of its kind from the scored rounds of 21.11.2026, read right, and
  round 67's misread trucker bid with the quote a right reading gives (`selection.implausible`).
- `scored-airline-bids.json`: a scored round with two priced airline bids, a sheet price and the scorer's verdict:
  among airlines the customer's wish decides, not the 50/50 rule of R15.

A row or variant may carry `known_gap`: the reference answer is right and the code is not yet. The note names what
the code does, why it matters for the price, and ends with `Owner: <role>.`. The benchmark reports it as a gap
instead of failing, so a fixture can land before its fix, and says when the case reads right: then delete the
note. `uv run python -m tools.benchmark_generalization --gaps` lists the open gaps by owner.

`tools/benchmark_generalization.py` prints every check; `tests/test_generalization.py` fails on anything it
reports as FAIL. Scanned sheets are read by the real scan reader with the model's answer replayed from
`tariffs/<carrier>-model-pages.json`, so both run offline and free.

The values come from the November 2026 start kit; replace or version them when the dataset changes.
