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
shipment with `crm/shipments.py` `find`. Without a reference, `described` reads the desk's description of the
shipment (origin airport or pickup postcode, destination, weight, company name) and narrows the shipments to that
destination by each of them; last it tries a customer address and a lane (`FRA to JFK`, `FRA-JFK`). `answer` writes, in sentences and in the asker's register (`greeting`: the name they signed with, formal, neutral
or casual), for up to five matches: weight, route (the pickup postcode for a door shipment), airline, status,
booking reference, the arrival from the ready date and the transit time (`details`: the ready date of the request
the shipment was quoted for, else the question's, read by `compose.ready_date`: only a date called ready), and our `PX-…` reference; signed by `PIXEL_SIGNER`. Below the prose, `record` repeats verbatim every line the answer
carried before (shipment, `Status: confirmed by <airline>, booked and ready to ship`, booking reference, transit,
customer address) until a variant is shown to score. No gateway call at all. Unknown shipment: we say we hold none under that
reference and ask for the booking reference. The reply goes to the sender, in the thread, through `core.outbox`.

## Pitfalls

- The tracking desk names no reference. It writes the shipment as the customer requested it: for a door-to-port
  request `<pickup postcode> to <destination>`, with the weight and the company name. The company is matched against
  the customer's address, the postcode against the request the shipment was quoted for.
- The status words come from the shipment (`booking`, `booked`, `confirmed`, `failed`); never promise a flight or a
  delivery date we do not hold.

## Open

- Q11: what makes an answer correct is unknown, and whether ours scores at all is not established (the board hides
  points per kind). We measure the total before and after each tracking answer (`bench/RUNS.md`) and change one
  thing per round, adding, never removing, until one variant moves it.
- No tracking question for a port-to-port shipment has been captured yet; that test uses a written mail.
