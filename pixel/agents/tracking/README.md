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

`agent.py` `handle`: find the shipment with `crm/shipments.py` `find` by any reference in the mail, else by customer
and lane; answer from the stored booking and shipment. No gateway call when a reference matches.

## Pitfalls

To be filled from the first real mails.

## Open

- Everything: this is the skeleton.
