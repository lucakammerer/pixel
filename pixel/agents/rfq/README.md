# rfq

## Mission

Turn a customer's request into one quote: the option they asked for, cheapest or fastest, with one all-in total and
its currency, sent as the first reply in their thread before the deadline. When the customer accepts, book it and
keep them informed.

## Rules

R1, R4, R9–R17, R22–R24, R26–R29, R31.

## Input / output

In: the RFQ `Message` and its `Deadline`; later the customer's reply and the airline's confirmation. Out: a `Quote`
(sent through `core/outbox.py` `send_quote`), a `Booking` and a `Shipment`. Emits `rfq.extracted`,
`quote.drafted`, `quote.sent`, `customer.accepted`, `customer.declined`, `booking.*`.

## Implementation

- `agent.py` `handle`: `extract.extract`; when rules read the request, `compose.prose` runs in parallel as the
  one gateway call of the quote (R28); when the model read it, that call already drafted the prose (`prose.raw`)
  and `compose.adopt` checks it. The prose may hold the quote at most 10 s after it started (`PROSE_WAIT_S`) and never past `target_at`; a late or
  failing call only drops the slots, the quote goes out with fixed phrases; `pricing/engine.py` `options`; `selection.select`; `compose.render`;
  `outbox.send_quote`. A request missing origin, destination or weight emits `error` and returns None; the
  orchestrator's fallback and the UI take over. Every drafted quote is saved to the CRM (`crm/quotes.py` `save`),
  the sent one again after `send_quote`, so booking finds it also when the fallback sent it.
- `agent.py` `fallback`: the RFQ and every `option.found` of the session, read back from the event log
  (`pricing/session.py`), combined and selected by code alone, no model call. Returns the quote unsent.
- `extract.py` `by_rules`: lane (`AAA-BBB`, `AAA to BBB`, `City (AAA) to City (BBB)`, `Origin:`/`Destination:`),
  weight, volume (or dimensions × pieces), pieces, fastest wording, door pickup with a 5-digit postcode, an explicitly
  requested currency, `de` or `en`. None unless exactly one lane and one weight are found.
- `extract.py` `extract`: `by_rules`, else exactly one `llm.ask_json` (`rfq.extract`); code validates the answer and
  lists what is missing in `Rfq.missing`. A request without an origin airport but with a pickup postcode takes the
  airport of the pickup zone as its origin (R16). Emits `rfq.extracted` with the whole `Rfq`.
- `compose.py` `render`: a reply (`Re:` the customer's subject) in English or German, written as prose: greeting
  with the customer's first name when they signed with it, an intro, one offer paragraph with the all-in total and
  its currency, carrier, transit, chargeable weight, what is included and the validity, a call to book, a closing
  line and a person's signature (`PIXEL_SIGNER`). Code formats every number and name; two phrasings per language
  alternate by session. `check` proves the finished mail holds the total exactly once with its currency, carrier
  and transit; on failure the mail is rendered without the model's slots (R9, P1).
- `compose.py` `answers` (R31): what the request asked beyond the price, answered by code in a paragraph of its
  own: the earliest arrival (`ready_date`: only a date the request calls ready, plus a day for a pickup, plus the transit), one air
  waybill for all lots, and that the drop-off cut-off follows with the airline's booking. Any doubt gives "".
- `compose.py` `prose` / `clean` / `adopt` (R31): the model writes three slots, `first_name`, `intro` and
  `closing`, on `PIXEL_MODEL_PROSE` when set. `clean` drops each slot that holds a digit, a three-letter code, a
  currency symbol, a URL, a weekday or month, a carrier's name, a greeting or sign-off, the wrong language, German
  `du`, or is too long; the first name must stand in the customer's mail. The checked slots are stored as
  `quote.prose`, so the fallback quote and the booking mails use them too.
- `booking.py` `handle_reply`: `intent` reads accept, decline or other by rules (English and German, a decline
  wins); anything else becomes a `human.note` for a person, no model call. `book`: one shipment (`PX-…`, our
  `team_reference`), one portal booking by `rate_id`; a lost connection is retried once (the portal answers 200 with
  the same booking), 410 fetches a fresh rate and books that, 503 or a quote without `rate_id` books by mail with the
  airline (address from the 503 message, else the carrier list), 404 and 409 are `booking.failed`. A second
  acceptance books nothing twice. The customer mails (booked, confirmed) are in the customer's language, with
  money and weight formatted by `compose`, the stored first name and the same signer, without a model call.
  `handle_confirmation`: "Booking <reference> confirmed" marks booking and shipment
  confirmed and tells the customer once.

## Pitfalls

- A request can list its shipment lot by lot, some figures per piece ("each 48 kg") and some for the lot ("together
  730 kg"), with net weights beside the gross ones. `extract.py` `_lots` adds gross weight, volume and pieces up in
  code, also when a model read the rest of the mail: a model's sum was 10% short and the quote was rejected as too low.
- A door-to-port request names the destination and the pickup address, not the origin airport.
- A request in German writes `1.250 kg` for 1250 kg and `12,5 kg` for 12.5 kg.
- Two weights in one mail (per piece and total) make the rules unsure: the model reads it.
- The prose slots must not contain a price or a code: the scorer reads the one total we write.
- The total is written like the portal's and the carriers' own: no thousands separator, a decimal point, also in a
  German mail. A grouped total can be read as a different number.
- Every step of the prose fails soft: an odd model answer, a missing name, a broken stored event or a rendering
  error each give the plain mail, never an exception on the quote or the fallback path.
- A reasoning model spends its output tokens on reasoning and can return empty slots: set `PIXEL_MODEL_PROSE` to a
  fast non-reasoning model.

## Open

- The quote is in the chosen rate's currency even when the customer asked for another (Q7).
