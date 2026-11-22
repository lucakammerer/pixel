# tracking

## Mission

Answer every tracking question from `tracking@hackloop.mail` about a shipment we booked, from our own CRM, in the
thread it came in. Never invent a status: what the CRM does not hold, we do not claim.

## Rules

R25, R30.

## Input / output

In: the tracking `Message`. Out: one reply through `core/outbox.py` `send`. Emits `tracking.answered` with the
shipment it matched, or that it matched none.

## Implementation

`agent.py` `match` identifies one shipment before including any record in the reply. Explicit airline and team
references must all identify the same shipment. Without a reference, `described` checks the destination, customer
evidence and origin airport or pickup postcode. A postcode requires a door-pickup quote. Exact postcodes take
precedence over a matching zone from a quote whose postcode was missing. Exact weights take precedence over
rounded matches within 1 kg or 1%, whichever is greater.

A message asking for tracking by a booking or team reference may open a new thread. `handle` only includes
shipment data for its original customer or the designated tracking desk. A reference alone does not establish
permission to see the shipment.

A customer asking in their own thread ("Where is our shipment?", routed here by `agents/rfq/followup.py`) means
that thread's shipment (`_own_shipment`) when nothing else identifies one.

With one match, `answer` gives the route, weight, booking status, airline and available reference and transit
details. A door-to-port route names the town of the pickup the question asks about (its zone's area in
`pickup_zones`), where ours was filled in from an earlier site; never a postcode or the customer's address.
The answer greets the asker by the name they signed with and signs off as the quote did (`compose.in_kind`): to the
town they greet from, or with their own "Viele Grüße" or "Cheers".
`details` uses the ready date to calculate the arrival when that date is available. `VARIANT` picks the answer:
`scored` (prose, then the record block with the customer's address), `a` (without the address, the pickup by town),
`b` (the block's facts in the prose, no block), `c` (`b` without its filler sentences). One variant per booked round, measured by the +10 on the board;
going back is that one line. Every variant carries our reference, the airline's, the airline, "booked and ready to
ship", transit days, lane and weight. If there are several plausible shipments or none,
the reply asks for a booking reference and includes no shipment records. The reply goes through the outbox
in the inbound thread and uses no gateway call, except for a question that is not in English: the English answer
is translated (`compose.localize`) and sent only when every reference, name and figure survives.

## Pitfalls

- Our record of a shipment can differ from the desk's description: the pickup postcode came from the customer's
  earlier site (same zone), or the zone's airport replaced the origin the customer named. `described` then matches
  by zone, or by company and weight alone, and still answers only for exactly one shipment.
- The tracking desk names no reference. It writes the shipment as the customer requested it: for a door-to-port
  request `<pickup postcode> to <destination>`, with the weight and the company name. The company is matched against
  the customer's address, the postcode against the request the shipment was quoted for.
- The reference can stand only in an attached enquiry: `question` gives `match` the subject, the body and every
  attachment code can read. A scan, a voice message, and an attachment that disagrees with the mail are open cells
  of the channel matrix (`tests/fixtures/channel-gaps.json`).
- The desk's wording is one template today; the same facts can come as "51804-DFW", "51804 → DFW", "1 795 kg" or
  "1795 kilos". `tools/benchmark_generalization.py` `TRACKING_CHANGES` changes every live question one way
  (section 14); each must find its shipment or ask for the reference, never name another.
- "ETD/ETA" has the shape of a lane written with "/" and once hid the real one after it. `_lane` takes the first lane
  whose destination one of our shipments flies to.
- The status words come from the shipment (`booking`, `booked`, `confirmed`, `failed`); never promise a flight or a
  delivery date we do not hold.

## Scoring evidence

`bench/RUNS.md` records tracking answers followed by a ten-point increase in the team total. Only the first
answer to a question counted in the observed rounds.
