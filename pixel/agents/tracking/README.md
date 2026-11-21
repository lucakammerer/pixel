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

`agent.py` `handle`: `match` looks for references in the mail (`BK-…` from the airline, `PX-…` ours) and finds the
shipment with `crm/shipments.py` `find`; without a reference it tries a customer address and a lane (`FRA to JFK`,
`FRA-JFK`). `answer` writes status, airline, lane, weight, booking reference and transit time from the stored
shipment, for up to five matches. No gateway call at all. Unknown shipment: we say we hold none under that
reference and ask for the booking reference. The reply goes to the sender, in the thread, through `core.outbox`.

## Pitfalls

- The status words come from the shipment (`booking`, `booked`, `confirmed`, `failed`); never promise a flight or a
  delivery date we do not hold.

## Open

- Q11: what the tracking agent asks and what makes an answer correct. Adjust `REFERENCE`, `LANE` and `answer` from
  the first real mails.
