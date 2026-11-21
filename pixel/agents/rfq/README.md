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

- `agent.py` `handle`: extract, price, select, write, send. `fallback`: the best stored option, by code alone.
- `extract.py` `by_rules`, `extract`: the request as an `Rfq`; rules first, one model call otherwise.
- `compose.py` `render`: the mail text. Numbers come from the `RateOption`, formatted by code.
- `booking.py` `handle_reply`: `intent` reads accept, decline or other by rules (English and German, a decline
  wins); anything else becomes a `human.note` for a person, no model call. `book`: one shipment (`PX-…`, our
  `team_reference`), one portal booking by `rate_id`; a lost connection is retried once (the portal answers 200 with
  the same booking), 410 fetches a fresh rate and books that, 503 or a quote without `rate_id` books by mail with the
  airline (address from the 503 message, else the carrier list), 404 and 409 are `booking.failed`. A second
  acceptance books nothing twice. `handle_confirmation`: "Booking <reference> confirmed" marks booking and shipment
  confirmed and tells the customer once.

## Pitfalls

To be filled from the first real mails.

## Open

- Everything: this is the skeleton.
