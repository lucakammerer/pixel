# Mail fixtures

One JSON file per stored mail, exactly as `GET /v1/mailbox/messages` returned it, captured with
`uv run python -m tools.replay --capture <message id>`. Name them by what they test (`rfq-door-to-port.json`,
`bid-odd-format.json`). Customer addresses are 5U's simulated agents; no keys, no real people.

`synthetic-*.json` are written by hand in the shape `pixel/api/mailbox.py` `to_message` assumes, for the kinds no
real mail has arrived of yet: a tariff mail, a tracking question, a request the rules cannot read. Replace each
with a captured mail of the same kind as soon as one arrives, then delete the synthetic one. `tests/test_router.py`
stores our own mails the captured ones answer (the bid request, the quote, a request to the rate desk) first.

A fixture may carry `content_base64` on an attachment; `tools/replay.py` serves it instead of downloading. The
benchmark's `CONTAINERS` (section 13) wraps each embedded file as html, a forwarded mail, a zip, without a suffix or
as other text, and reads it again against the same hand-read answers.

`tools/kpi.py` replays each `rfq-*` and `synthetic-rfq-*` fixture, plus any names listed in
`tests/fixtures/kpi-expectations.json`. A misrouted request stays in the denominator. An answer requires a
`quote.sent` receipt from the replay mailbox. Each row reports routing, delivered price and currency, and whether
the price covers all requested services. The expectation file pins the amount, currency and service scope for
selected cases. Offline replay advances a virtual clock through bid waits and deadlines.

Captured carrier answers, each to a request in the wording of `pixel/pricing/bids.py` `_subject` and `_body`:

- `bid-decline-lane.json`: an airline that serves the destination but not the lane declines and lists the lanes it
  flies.
- `bid-airline-priced.json`: an airline's all-in total (written without a thousands separator), transit days, a
  validity time and a reference to quote when booking.
- `bid-airline-priced-labelled.json`: the same from an airline that is neither in a sheet nor on the portal, with
  transit and validity on labelled lines of their own. The carriers' wording differs from mail to mail.
- `bid-pickup-priced.json`: a trucker's pickup price, a pickup time in hours, a validity time and a quote reference.
- `bid-pickup-in-attachment.json`: round 63, a trucker's pickup offer as a pdf table (embedded), the body only
  "you find our price offer in the attachment"; total, base and weight charge, lead time in hours.
- `ratedesk-portal-rates.json`: the rate desk's answer, a thread of its own that names our mail by its subject: the
  portal's rate per airline on the lane with its rate id and validity, and the address of an airline that is not on
  the portal.

`synthetic-novel-requirement.json` is `rfq-port-to-port.json` with two requirements no rule reads: only one airline,
at JFK by a date, and the customs documents named (`tests/test_novelty.py`).

Captured mails of a request, from the request to the confirmed booking:

- `rfq-door-no-origin.json`: the first scored request. Door-to-port, fastest, a currency asked for; it names the
  destination and the pickup postcode but no origin airport.
- `rfq-door-offer-as-pdf.json`: round 76. Door-to-port, fastest; the customer asks for the offer as a PDF
  attachment and declined our quote without one (`no_pdf_offer`).
- `rfq-dg-packing-list-as-xls.json`: `rfq-dg-packing-list.json` with its packing list saved as Excel 97 (`.xls`, the
  mailbox allows it, R6), cell for cell; written once with xlwt, which the project does not depend on.
- `rfq-door-request-in-pdf.json`, `rfq-door-request-in-docx.json`, `rfq-door-request-in-pdf-fastest.json`: rounds 67,
  68 and 75. The mail only points to `quotation-request.pdf` or `.docx` (embedded), which holds the whole request.
- `rfq-door-request-in-txt.json`: round 66. The mail only points to `quotation-request.txt` (embedded), which
  holds the whole request: destination, weight, volume, pieces, pickup postcode, the wish. Read by rules live.
- `rfq-sli-volume-differs-from-mail.json`: round 33, MUC-CAI, cheapest. The mail says 2.78 cbm; the attached packing
  spec (embedded) says 1.92 cbm and its table bears that out. The scorer priced 1.92 and rejected our quote on
  2.78 as too high.
- `rfq-packing-list-volume-differs-from-mail.json`: round 34, MUC-GRU, fastest, the same kind in another layout: a
  "Packing List" whose table starts with the length. The mail says 11.69 cbm (140 x 100 x 167 cm), the list 7.49
  (140 x 100 x 107 cm); quoted on the mail's before #159 was live, rejected as too high.
- `rfq-spec-weight-differs-from-mail.json`: round 35, MUC-SGN, fastest. The mail says 2644 kg; the attached xlsx
  spec (embedded) says 1751 kg in its total and its one table row. Quoted on 2644, rejected as too high.
- `rfq-excluded-airline.json`: round 42, FRA-ULN door to port, cheapest. The customer rules out one airline for this
  and every later request; we quoted it as the cheapest bid and were rejected as `excluded_airline`.
  `../scored-excluded-airline.json` holds the bids and the verdict.
- `rfq-door-packing-weight-differs-from-mail.json`: round 57, door to port (73825 Esslingen) to EZE, cheapest. Two
  shapes seen apart before, now in one mail: a "RE: FW:" subject and an "-----Original Message-----" below that
  belong to an old VIE-RUH request (708 kg), and an attached packing list (embedded) whose 1729 kg contradicts the
  mail's 1210 kg, its table's columns in another order (weight, width, length, height, pieces). Quoted on 1729 kg,
  accepted.
- `rfq-door-two-shipments.json`: round 113, door to port from Munich, fastest, one numbered line per shipment
  (RUH 749 kg, DXB 1239 kg). The model read no one destination and we asked for it before the quote.
- `rfq-port-to-port.json`: a request that names both airports and delivers to the airport itself; the rules read
  it without a model.
- `rfq-port-cheapest-airline-bids.json`: round 28, port to port and cheapest, sent while the portal's rate API
  answered 503. Two portal airlines priced it by mail; `../scored-airline-bids.json` holds their answers and the
  scorer's verdict on our quote.
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

Every captured request (`rfq-*.json`) has its reference row in `../rfqs/live.jsonl`, written from the mail by a
person (`tools.benchmark_generalization --stub`). It is read by `extract.py` `by_rules` without a model;
`tests/test_extract.py` checks each one:

- `rfq-door-trucking.json`, `rfq-door-ready-for-pickup.json`: door-to-port, the destination only, the pickup site by
  postcode and "arrange trucking from our site".
- `rfq-lots-two.json`: two lots of one piece each; "ready for pickup", yet the customer delivers to the airport.
- `rfq-lots-gross-after.json`: two lots whose weight and volume are for the whole lot, without "each" or
  "together"; the scorer accepted their sum.

`customer-declines-too-low.json`: the customer's decline of our too-low quote for `rfq-door-three-lots.json`, worded
"we will not go ahead with it". `booking.py` `intent` treats this as a decline.

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
`rfq-door-quoted-old-request.json`: round 14. A new door-to-port request (42401 Wuppertal to PEK) as "RE: FW:" of
an old one, which is quoted below it with its own lane and weight (ZRH to MEL, 3,459 kg). The model read it and
guessed origin CGN; zone 42 is DUS, no airline flew CGN-PEK, the request was missed.
Our reply takes the name, the tone and the ready date from the customer's own words only
(`pixel/agents/rfq/extract.py` `own_words`): the old mail below says "Dear team".

`synthetic-rfq-facts-in-attachment.json`: round 10's request with its weight sentence replaced by "All details are
in the attached packing specification". Weight and volume stand only in the attachment.

`rfq-door-warehouse-no-postcode.json`: round 15, rejected too low. "RE: FW:" with an old request quoted below;
pickup "from our warehouse" with no postcode or town, only the signature "Best greetings from Regensburg". The zone 93
truckers answered "To quote we need: the pickup postcode", so the quote went out without a pickup. Asked again by
hand, one of them priced the zone's first postcode and a postcode of the town alike.

`rfq-door-warehouse-known-customer.json`: round 16, missed. "Truck pickup from our warehouse" with no place at all
and no signature town. The customer (Nordlicht Audio) had named "our site in 36627 (Fulda)" in round 9.

`rfq-door-warehouse-earlier-sites.json`: round 17, missed (it arrived 11 s before the restart onto the fix). "Truck
pickup from our warehouse", no place; this customer (Lindqvist Medtech) had named 51447 in round 1 and 21663 in
round 11: the latest one counts.

`rfq-door-warehouse-two-sites.json`: round 18, pickup "from our warehouse"; the customer had named two sites before.
`customer-names-pickup.json`: her answer when asked afterwards: "The pickup address is 51115 Bergisch Gladbach".
`rfq-door-warehouse-one-site.json`: round 19, same wording from a customer with one earlier site (51896): accepted.
`rfq-door-warehouse-wrong-site.json`: round 20, same wording; the customer's one earlier site (61391) was wrong.
`customer-names-pickup-town.json`: his answer when asked: "The pickup is in Augsburg, Germany" (town only, zone 86).
`tests/fixtures/warehouse-rounds.json` lists rounds 15 to 19 with what we read, what we quoted, the verdict and the
bound it gives on the scorer's best price.

`rfq-voice-unread.json`: round 69, the whole request was a voice message we could not read, so we asked.
`customer-answers-every-fact.json`: the answer 13 s later with every fact, door-to-port to JFK; "select the departure
airport for the fastest option to JFK" read JFK as an origin and the answer was dropped.
