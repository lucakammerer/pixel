# tariffs

## Mission

Turn every tariff an airline sends, in whatever file type and layout, into rows of the tariff store with version
and validity, without a person. A sheet that cannot be read with certainty is stored as failed and shown in the UI,
never used for pricing.

## Rules

R12–R14, R33, R35.

## Input / output

In: a tariff mail with attachments, or a folder (the start kit). Out: `LaneTariff` rows through
`pricing/store.py` `upsert`. Emits `tariff.ingested` with carrier, version and lane count (and `fx_eur` when a
non-EUR sheet or its mail states one), or `tariff.failed` with the findings of `check`.

## Implementation

- `parse`: every text format becomes lines (`_lines`: xlsx rows, docx paragraphs and tables in order, PDF text,
  txt; a voice message, mp3, wav, m4a or ogg, one line per spoken sentence by a local `faster-whisper` model
  (`_transcribe`, no gateway spend, about a second per minute of speech; its ~150 MB download goes to the pixel
  user's Hugging Face cache; the live process loads it at start, `app.py` `_preload_speech`), one line per
  sentence, with spoken prices and small numbers written as figures: "340 euros and 60 cents" as 340.60 EUR, "four
  hours" or "vier Stunden" as 4 hours (`_spoken`)), and one reader (`_read`) interprets them: title (`<carrier> | ...`), `Currency`, `Valid from/to` (also on one
  line, `Valid 01.11.2026 - 30.11.2026`; any of the date formats in `DATE_FORMATS`), the rate unit (`All rates are
  per 100 kg` divides every rate by 100), `Origin:` headings, a break header (`from 0 kg`, `-45`/`+45`, `0 kg+`; `-45`
  is the 0 kg break; `Fuel`/`Security` in the header mean surcharge columns), table rows (lane, transit and minimum
  in the order the last header naming both gives them, `LEAD_COLUMN`, one rate per break), lane blocks (`Transit`, `Minimum`, `from N kg R`, `Fuel`, `Security` on their own lines,
  across page breaks) and surcharge sentences (`<lane>: fuel X per kg, security Y per kg`). A lane is the first two
  IATA codes before the first number, or one code under an `Origin:` heading. A row with the wrong count of numbers
  refuses the whole file (ValueError) instead of guessing.
- `_ingest` first tries the text parser for PDFs. When it cannot read a scan or layout, `_pdf_pages` renders the
  pages as JPEGs. `read_with_model` sends those pages or the original image attachments through `llm.ask_json`,
  once per page. The model transcribes cells. Code merges rates and surcharges across pages, parses numbers
  and applies the same validation as for text tariffs before making rates available to pricing.
- `check`: on every result: lane codes, currency, dates, minimum > 0 and not a transit in days (a whole number up to
  30 below two kg at the first rate), transit 1 to 30 days, first break at 0 kg,
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
  swapped read the minimum as transit days, and `check` only noticed when the numbers left its ranges. `_read`
  takes their order from the header, also when it stands one row above the breaks (`Destination  Transit  Minimum`
  over `-45  +45 ...`); `check` refuses a minimum that looks like days.
  `tests/fixtures/tariffs/variants/` holds sheets with one change each; every change must be parsed right or
  refused, never parsed into wrong values.
- A sheet in another currency than EUR parsed, but sheet pricing had no exchange rate for it and dropped the sheet.
  `_ingest` records a rate the sheet or its mail states ("1 USD = 0.92 EUR") as `fx_eur` on `tariff.ingested`;
  `pricing/engine.py` `_fx_eur` takes that, else the newest `fx_eur` the portal quoted for the currency, never a
  constant. Without either the sheet is not priced (`option.failed`).
- The sheet's sentences are rules: rates per 100 kg instead of per kg read 100 times too high until `RATE_UNIT`
  divided them. The model's page reader copies cells and does not see that sentence. A minimum on freight plus
  surcharges has no field in `LaneTariff` yet and is read as the kit's rule (wrong only where the minimum binds).
- A refused text, xlsx or docx sheet is dropped (`tariff.failed`), not read by the model: the carrier's older
  version keeps pricing. A minimum written `1,250.00` and a remarks column holding a number are refused today
  (`tests/fixtures/tariffs/variants/`).

## Open

- Unrecognised non-PDF text layouts are reported as failed. There is currently no model transcription path for them.
