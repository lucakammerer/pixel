# tariffs

## Mission

Turn every tariff an airline sends, in whatever file type and layout, into rows of the tariff store with version
and validity, without a person. A sheet that cannot be read with certainty is stored as failed and shown in the UI,
never used for pricing.

## Rules

R12–R14, R33, R35.

## Input / output

In: a tariff mail with attachments, or a folder (the start kit). Out: `LaneTariff` rows through
`pricing/store.py` `upsert`. Emits `tariff.ingested` with carrier, version and lane count, or `tariff.failed` with
the findings of `check`.

## Implementation

`agent.py` `parse` (code parsers for xlsx, docx, txt, text PDF), `read_with_model` (images and layouts `parse`
refuses, one gateway call per document), `check` (code checks on every result), `handle` (a tariff mail),
`ingest_folder` (the start kit, including the carrier list and the pickup zones).

## Pitfalls

- Decimal commas stored as text in one sheet, real numbers in the next; dates in a different format per sheet.
- Weight breaks differ per airline; the header names them (`from 300 kg`, `+300`, `300 kg+`), and the first break
  appears as `from 0 kg` or `-45`.
- Surcharges come as columns, as a second table or as one sentence per lane below the rates.
- Lanes are written as codes, as city names with codes, or grouped under an origin heading.
- The scanned sheet is skewed and split over two pages: rates on one, surcharges on the other.

## Open

- Everything: this is the skeleton.
