# Mail fixtures

One JSON file per stored mail, exactly as `GET /v1/mailbox/messages` returned it, captured with
`uv run python -m tools.replay --capture <message id>`. Name them by what they test (`rfq-door-to-port.json`,
`bid-odd-format.json`). Customer addresses are 5U's simulated agents; no keys, no real people.

`synthetic-*.json` are written by hand in the shape `pixel/api/mailbox.py` `to_message` assumes, for the kinds no
real mail has arrived of yet: a tariff mail, a tracking question, a request the rules cannot read. Replace each
with a captured mail of the same kind as soon as one arrives, then delete the synthetic one. `tests/test_router.py`
stores our own mails the captured ones answer (the bid request, the quote, a request to the rate desk) first.

A fixture may carry `content_base64` on an attachment; `tools/replay.py` serves it instead of downloading.

Every fixture the router places as a customer's request is replayed by `tools/kpi.py` in each pull request; its
price on the base and on the branch is compared there, offline.

Captured carrier answers, each to a request in the wording of `pixel/pricing/bids.py` `_subject` and `_body`:

- `bid-decline-lane.json`: an airline that serves the destination but not the lane declines and lists the lanes it
  flies.
- `bid-airline-priced.json`: an airline's all-in total (written without a thousands separator), transit days, a
  validity time and a reference to quote when booking.
- `bid-airline-priced-labelled.json`: the same from an airline that is neither in a sheet nor on the portal, with
  transit and validity on labelled lines of their own. The carriers' wording differs from mail to mail.
- `bid-pickup-priced.json`: a trucker's pickup price, a pickup time in hours, a validity time and a quote reference.
- `ratedesk-portal-rates.json`: the rate desk's answer, a thread of its own that names our mail by its subject: the
  portal's rate per airline on the lane with its rate id and validity, and the address of an airline that is not on
  the portal.

Captured mails of a request, from the request to the confirmed booking:

- `rfq-door-no-origin.json`: the first scored request. Door-to-port, fastest, a currency asked for; it names the
  destination and the pickup postcode but no origin airport.
- `rfq-port-to-port.json`: a request that names both airports and delivers to the airport itself; the rules read
  it without a model.
- `customer-accepts.json`: the customer's acceptance of our quote, 25 seconds after it, in the quote's thread.
- `customer-thanks.json`: the customer's answer to our booking notice; nothing to do, no acceptance.
- `booking-confirmed.json`: the airline's confirmation, a thread of its own about 40 seconds after the booking, with
  the booking reference, our reference, lane and price.

`tracking-door-to-port.json` is the tracking desk's question about a door-to-port shipment: no reference, the
shipment described by pickup postcode, destination, weight and company.

`rfq-door-three-lots.json` is a door-to-port request in three lots with mixed per-piece and per-lot figures; our
first quote for it was rejected as too low.

`rfq-door-with-packing-list.json` is a customer's request with a packing specification attached (the file itself
is not stored); the router once took it for a tariff mail.

Every captured request (`rfq-*.json`) is read by `extract.py` `by_rules` without a model; `tests/test_extract.py`
checks each one:

- `rfq-door-trucking.json`, `rfq-door-ready-for-pickup.json`: door-to-port, the destination only, the pickup site by
  postcode and "arrange trucking from our site".
- `rfq-lots-two.json`: two lots of one piece each; "ready for pickup", yet the customer delivers to the airport.
- `rfq-lots-gross-after.json`: two lots whose weight and volume are for the whole lot, without "each" or
  "together"; the scorer accepted their sum.

`customer-declines-too-low.json`: the customer's decline of our too-low quote for `rfq-door-three-lots.json`, worded
"we will not go ahead with it". We booked it anyway: `booking.py` `intent` read "go ahead" as an acceptance.

Requests with dangerous goods, captured with their attachment (`content_base64`, fetched from the API: `--capture`
stores the mail without the file). The mail says "general cargo, non-dangerous"; the attachment declares them:

- `rfq-dg-packing-list.json`: an xlsx packing specification with "Dangerous goods: UN1133, Class 3, Adhesives".
  Our quote did not mention it and was rejected (`dg_docs_missing`); `customer-declines-dg.json` is the decline.
- `rfq-dg-instruction-bullets.json`: a pdf shipper's letter of instruction with "UN1950, Class 2.1, Aerosols,
  flammable", the request written as labelled bullets. A quote at the normal price that named the goods and asked
  for the DGD and the MSDS was accepted.

`rfq-import-no-pickup.json`: the buyer abroad imports, our customer delivers to the airport, "so there's no pickup
needed". Quoted like any export from the named airport; accepted.

`rfq-door-import-two-towns.json`: door-to-port from 24983 (Kiel), signed "Best greetings from Regensburg", another
zone's town. Read by the postcode; without one, a pickup town is taken only when the mail names one zone's town.

`rfq-door-forwarded-thread.json` is a door-to-port request written above a forwarded old thread, under that
thread's subject; the old thread's weight, volume and airports must not be read.
