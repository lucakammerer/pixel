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

- `parse`: every text format becomes lines (`_lines`: xlsx rows, docx paragraphs and tables in order, PDF text,
  txt), and one reader (`_read`) interprets them: title (`<carrier> | ...`), `Currency`, `Valid from/to` (any of
  the date formats in `DATE_FORMATS`), `Origin:` headings, a break header (`from 0 kg`, `-45`/`+45`, `0 kg+`; `-45`
  is the 0 kg break; `Fuel`/`Security` in the header mean surcharge columns), table rows (lane, transit, minimum,
  one rate per break), lane blocks (`Transit`, `Minimum`, `from N kg R`, `Fuel`, `Security` on their own lines,
  across page breaks) and surcharge sentences (`<lane>: fuel X per kg, security Y per kg`). A lane is the first two
  IATA codes before the first number, or one code under an `Origin:` heading. A row with the wrong count of numbers
  refuses the whole file (ValueError) instead of guessing.
- `_ingest` first tries the text parser for PDFs. When it cannot read a scan or layout, `_pdf_pages` renders the
  pages as JPEGs. `read_with_model` sends those pages or the original image attachments through `llm.ask_json`,
  once per page. The model transcribes cells. Code merges rates and surcharges across pages, parses numbers
  and applies the same validation as for text tariffs before making rates available to pricing.
- `check`: on every result: lane codes, currency, dates, minimum > 0, transit 1 to 30 days, first break at 0 kg,
  breaks ascending, rates positive and never rising with weight, surcharges below the first rate.
- Version: `<valid from>#<sha256 of the file, 8 chars>`, so a re-sent identical file changes nothing and a new
  version of the same month is a new row. `store.lookup` takes per carrier the newest version valid on the day.
- `ingest_folder` and `handle`: CSVs with `name,kind,email,serves` go to the carrier list (any file name, so
  `carriers.csv` works too), `zone,area,airport` to the pickup zones; `name-p1.jpg`, `name-p2.jpg` are one
  document. A scan already in the store (`store.has_source`) is not read again. Results are `tariff.ingested` or
  `tariff.failed` with the findings; a failed sheet is never stored.

## Pitfalls

- Decimal commas stored as text in one sheet, real numbers in the next; dates in a different format per sheet.
- Weight breaks differ per airline; the header names them (`from 300 kg`, `+300`, `300 kg+`), and the first break
  appears as `from 0 kg` or `-45`.
- Surcharges come as columns, as a second table or as one sentence per lane below the rates.
- Lanes are written as codes, as city names with codes, or grouped under an origin heading.
- The scanned sheet is skewed and split over two pages: rates on one, surcharges on the other.
- `check` cannot know whether a code is a real airport; it only checks the form.
- A sheet read by column position turns into wrong values when an update reorders its columns: Minimum and Transit
  swapped read the minimum as transit days, and `check` only notices when the numbers leave its ranges.
  `tests/fixtures/tariffs/variants/` holds sheets with one change each; every change must be parsed right or
  refused, never parsed into wrong values.
- A sheet in another currency than EUR parses, but sheet pricing has no exchange rate for it and drops the sheet.

## Open

- Unrecognised non-PDF text layouts are reported as failed. There is currently no model transcription path for them.
