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

- `agent.py` `handle`: `extract.extract`; when rules read the request, `compose.opening` runs in parallel as the
  one gateway call of the quote (R28); `pricing/engine.py` `options`; `selection.select`; `compose.render`;
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
- `compose.py` `render`: subject and body in English or German, one all-in total with its currency formatted by
  code, carrier, transit, chargeable weight, what is included, validity when the rate has one. `opening`: one
  sentence from the model; code drops it if it holds a digit or a three-letter code, and any failure gives None.
- `booking.py` `handle_reply`: `intent` reads accept, decline or other by rules (English and German, a decline
  wins); anything else becomes a `human.note` for a person, no model call. `book`: one shipment (`PX-…`, our
  `team_reference`), one portal booking by `rate_id`; a lost connection is retried once (the portal answers 200 with
  the same booking), 410 fetches a fresh rate and books that, 503 or a quote without `rate_id` books by mail with the
  airline (address from the 503 message, else the carrier list), 404 and 409 are `booking.failed`. A second
  acceptance books nothing twice. `handle_confirmation`: "Booking <reference> confirmed" marks booking and shipment
  confirmed and tells the customer once.

## Pitfalls

- A request can list its shipment lot by lot, some figures per piece ("each 48 kg") and some for the lot ("together
  730 kg"), with net weights beside the gross ones. `extract.py` `_lots` adds gross weight, volume and pieces up in
  code, also when a model read the rest of the mail: a model's sum was 10% short and the quote was rejected as too low.
- A door-to-port request names the destination and the pickup address, not the origin airport.
- A request in German writes `1.250 kg` for 1250 kg and `12,5 kg` for 12.5 kg.
- Two weights in one mail (per piece and total) make the rules unsure: the model reads it.
- The opening sentence must not contain a price or a code: the scorer reads the one total we write.
- The total is written like the portal's and the carriers' own: no thousands separator, a decimal point, also in a
  German mail. A grouped total can be read as a different number.

## Open

- The quote is in the chosen rate's currency even when the customer asked for another (Q7).
