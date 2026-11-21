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
price on the base and on the branch is compared there.

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
