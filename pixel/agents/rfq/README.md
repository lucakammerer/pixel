# rfq

## Mission

Turn a customer's request into one quote: the option they asked for, cheapest or fastest, with one total and
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
  failing call only drops the slots, the quote goes out with fixed phrases; `pricing/engine.py` `options` (bids by
  mail, pickup and airline, are waited for until all are in, or until the later of `target_at` and
  `PIXEL_BID_WAIT_S` after the ask, never past `hard_at`: `pricing/bids.py` `collect`); `selection.select`; `compose.render`;
  `outbox.send_quote`. A request missing origin, destination or weight emits `error` and returns None; the
  orchestrator's fallback and the UI take over. Every drafted quote is saved to the CRM (`crm/quotes.py` `save`),
  the sent one again after `send_quote`, so booking finds it also when the fallback sent it.
- `agent.py` `served_airports` accepts codes named in the current mail or its attachments, case-insensitively,
  and codes known locally. Only an unserved guess triggers a time-limited portal check and, if still unresolved,
  one optional `rfq.airports` call. Quoted older correspondence is excluded.
- `agent.py` `fallback` reads the RFQ and the stored options on its current lane. A late complete option can be
  sent while the session is automatic. A door request waits for a priced trucker until `hard_at`. If pickup is
  still unpriced at that point, the reply states the airport-to-airport price and excludes pickup. A zone hint
  used for trucker bids stays separate from the customer's address. The quote asks for the address and postcode
  when the postcode is missing, including when the quoted pickup price is for the zone pending address confirmation.
- `pricing/bids.py` `parse` binds the amount to its label and unit. `pricing/calc.py` multiplies rates per kg by
  chargeable weight, preserves exchange-rate precision during conversion and rounds transit in hours up to whole
  days. `pricing/engine.py` starts carrier sourcing when the portal is slow or unavailable.

- `extract.py` `by_rules`: lane (`AAA-BBB`, `AAA to BBB`, `City (AAA) to City (BBB)`, `Origin:`/`Destination:`),
  weight, volume (or dimensions × pieces), pieces, fastest wording, door pickup with a 5-digit postcode, an explicitly
  requested currency, `de` or `en`. None unless exactly one lane (for a door pickup with a postcode: one destination)
  and one weight are found.
- `extract.py` `extract`: `by_rules`, else exactly one `llm.ask_json` (`rfq.extract`); code validates the answer and
  lists what is missing in `Rfq.missing`. A request without an origin airport but with a pickup postcode takes the
  airport of the pickup zone as its origin (R16), and so does a door-to-port request whose origin is an airport no
  pickup zone trucks to; when the zone has none, the model reads the mail. Emits `rfq.extracted` with the whole `Rfq`.
- `compose.py` `render`: a reply (`Re:` the customer's subject) in English or German, in one personal voice for every
  customer, formal or not (`TEXT[lang]["warm"]`, R31): every team's quote reads like the same clean template, and
  the jury picks the mail that reads most human. "Hi Anna!" by the first name they signed with (`greeting`: the
  model's slot, else `signed_name` by rule); the model's opening on a line of its own when it passed, else none (no
  stock opening); the offer with an opinion ("I'd go with …", "Good news: …"): total and currency, carrier, transit,
  what is included, a short line ("Simple as that."), the chargeable weight only when it is above the gross weight;
  the validity on our desk's clock ("until 17:22 today") with the booking line; the signature by first name
  (`signature`) and a P.S. that only says what we do anyway. At most three smileys, none twice (`_one_each`).
  "Hope your week's going well" is answered first (`WELL`, "All good here, thanks for asking!"); `sign_off` answers
  the customer's own closing (`in_kind`): a wish for the day is returned, and the sign-off goes to the town they
  greet from, or reads "Cheers" or "Viele Grüße" when theirs does; without one, "Best wishes". Code formats every
  number and name; two to three phrasings per sentence, picked by session, so two quotes do not read alike. `check`
  proves the finished mail holds the total exactly once with its currency, carrier and transit; on failure the mail
  is rendered without the model's slots (R9, P1).
- `compose.py` `answers` (R31): what the request asked beyond the price, answered by code in a paragraph of its
  own: the earliest arrival (`ready_date`: only a date the request calls ready, plus a day for a pickup, plus the
  transit; in the city the customer wrote behind the airport's code, `city_of`), the departure that goes with it
  when they ask for the earliest departure, the pickup date said outright when they ask for the earliest pickup
  (`PICKUP_ASK`), one air
  waybill for all lots, and that the drop-off cut-off follows with the airline's booking. Any doubt gives "".
- `compose.py` `prose` / `clean` / `adopt` (R31): the model writes three slots, `first_name`, `intro` and
  `closing`, on `PIXEL_MODEL_PROSE` when set, plus `hook`: the phrase of the customer's mail the opening answers (a
  friendly remark, an attachment, their buyer, the reason for the request), or null, and then no opening. It also
  sees the openings this customer already got (`_history`, `notes`). `_opening_ok` keeps the opening only when the
  hook stands in the mail and is not the small talk `WELL` answers, it has at most `WORDS_MAX` words, no capitalised
  word of the mail (a name or place), and its first two words differ from every earlier opening to this customer;
  `ECHO` drops fillers and echoes ("thank you for your request", "inquiry", "regarding", "I've noted", "here is",
  "cheapest", "pickup", announcing the offer) and a second sentence. A dropped opening is stored in `quote.prose`
  as `rejected`; `tests/fixtures/openings.json` holds every live request with the opening we sent. The
  extraction's slots (model path) carry no hook, so those quotes open with the price. The closing slot is no
  longer rendered. `clean` drops each slot that holds a digit, a three-letter code, a
  currency symbol, a URL, a weekday or month, a carrier's name, a greeting or sign-off, the wrong language, German
  `du`, or is too long; the first name must stand in the customer's mail. The checked slots are stored as
  `quote.prose`, so the fallback quote and the booking mails use them too.
- `booking.py` `handle_reply` checks the customer's own words for an affirmative booking instruction or decline.
  Questions, conditional language and replies to unsent drafts are left for the operator with `human.note`.
  A delivered offer can be booked even when it went out through the deadline fallback.
- `book` uses a shipment reference for the portal booking, including a retry. An expired rate is refreshed for
  the selected billed weight. A 503 or an option without a rate id goes to the airline by mail. The carrier
  receives the air purchase price. The customer's booking notice states the accepted quote total, including
  any priced pickup, and names the excluded pickup on an airport-only booking. Confirmation updates that shipment. Replay uses a local mailbox and blocks portal bookings.
  Booking and confirmation mails use the quote's greeting (`compose.greeting`), sign-off (`compose.sign_off`) and
  signature (`compose.signature`); the booking mail ends on a thank-you (`booking.THANKS`).

- `check.py`: the last look at every quote `_quote` drafts, the fallback's too, by code alone and independent of how
  the quote was built. `check.run` re-derives what it can from the customer's mail, the RFQ, the session's options
  and the finished text. BLOCK: lane, arithmetic of the parts, chargeable weight, pickup present exactly when asked
  for (round 15), the lots added up again (round 9), a priced weight no plain reading of the mail's own mass
  figures gives (`_accounting`: kg, lb and t converted, net, limits, a truck's or pallet's capacity and an earlier shipment left out, per-piece figures times their
  count; a listed shipment only by its sum; a mail with no weight figure only warns), dangerous goods marked by `extract` without the papers
  (round 10), the total exactly once and no other amount with a currency outside the DG note, template leftovers.
  WARN: a total more than 5% from the cheapest the session had (round 16), DG words the RFQ did not mark, an
  implausible price per kg, the language, an expired rate. The check never holds or changes a quote; every result
  is a `quote.checked` event and a BLOCK is an issue for `feedback.alert`. `PIXEL_PRECHECK=off` skips it. A rule
  that raises becomes a WARN finding.

## Pitfalls

- A customer's yes comes in many shapes ("Bitte buchen Sie.", "Please book it and send us the AWB once
  available.", "Could you book it, please?"). `booking.py` `intent` takes an acceptance anywhere in a sentence that
  is no condition and no question other than one asking us to book; a decline wins first. Read too strictly, a yes
  goes to a person, nothing is booked and the tracking bonus is lost.
- A request can sit above a forwarded or quoted older mail with its own greeting, name and dates: the reply's
  name, sign-off and answers come from the customer's own words only (`extract.py` `own_words`, the same cut pricing reads).
- A request can carry a packing specification or a shipper's letter of instruction that declares dangerous goods
  ("Dangerous goods: UN…, Class …") while the mail calls the goods non-dangerous. `extract.py`
  `dangerous_goods` reads them and marks the request; `agent.py` `_noted` then names the goods in the quote and asks
  for the shipper's declaration and the safety data sheet (`dangerous.py`), in the deadline fallback too. The
  note points to the attachment, or to the mail when the mail itself declared them. Without that the scorer rejects the quote (`dg_docs_missing`); with it, at the unchanged price,
  it accepted one.
- When the model reads a request, it copies each piece group as written (`lots`: pieces, weight and unit, per piece
  or not, volume or one piece's dimensions) and `extract.py` `_model_lots` adds them up. Its own total for the
  three-lot mail without labels was 1094 kg against 1952 kg. A weight in pounds is converted by code.
- A request can list its shipment lot by lot, some figures per piece ("each 48 kg") and some for the lot ("together
  730 kg"), with net weights beside the gross ones. `extract.py` `_lots` adds gross weight, volume and pieces up in
  code, also when a model read the rest of the mail: a model's sum was 10% short and the quote was rejected as too low.
- A door-to-port request names the destination and the pickup address, not the origin airport. The rules read it
  when it names one destination and a pickup postcode; the origin is the zone's airport.
- A model asked for the origin of a door-to-port request guesses an airport from the town's name, one no airline
  flies from: every carrier declined and the request was missed. `extract.py` `_zone_origin` takes the zone's
  airport instead of an origin that is not the airport of any pickup zone.
- "Pickup from our warehouse" can name no place (rounds 15 to 18 lost). The customer holds the fact back and gives
  it when asked: asked after round 18, Lindqvist answered "51115 Bergisch Gladbach" within 10 s. `extract.py`
  `_town_zone` takes a town on the line that describes the pickup. Otherwise `_ask` sends one short question in the
  thread for exactly the facts `_unknown` lists (pickup place, origin, destination, weight, volume: without one
  the chargeable weight is the gross weight, too low for bulky goods), never a price, and the
  request is read again with the answer (a town alone maps to its zone). No answer 60 s after the request: the
  first-named earlier site, then a town anywhere in the mail. The history was right once (round 19) and wrong twice
  (rounds 18 and 20), so it is never used without asking. Whether the scorer counts the question as our first reply is not known yet.
- Dimensions in inches ("5 skids 48x40x50 in") were read as centimetres: 0.1 cbm instead of 7.9. A unit of in, inch
  or inches is converted by code ("in" only before punctuation or the line end: "120x80x150 in total" is cm); no
  unit is centimetres. Skids, crates, drums, cases, Packstücke and Kisten count
  as pieces.
- "Gross weight: 480 lbs" was read as 480 kg. Weights in pounds or tonnes alone are converted by code; beside a
  weight in kg, the kg wins.
- A gateway that refuses the model or fails left a request without an `Rfq`: nothing to quote, not even by the
  fallback. `extract` then takes `by_rules(guess=True)`, the largest gross weight and volume the mail names,
  `extracted_by: guess`, and emits `error`; with no lane at all it still raises.
- A request can come as a reply or forward with an older request quoted below it ("-----Original Message-----"),
  and the subject of that older mail. `extract.py` `_text` reads neither: round 14's quoted lane and weight sent it
  to the model.
- A model guesses a door-to-port origin from the town (XFW for Lueneburg, CGN for Wuppertal): `_zone_origin` takes
  the pickup zone's airport unless the mail names the origin code itself.
- A mail can supply the weight while a packing list supplies volume, dimensions or piece count. `read_request`
  combines fields separately, giving precedence to an explicit figure in the mail. A unique attachment value
  can supply a missing field. One exception, round 33: the mail said 2.78 cbm (120 x 40 x 116 cm), the attached
  packing spec 1.92 cbm with a table row of 5 pieces of 40 x 120 x 80 cm, and the scorer priced 1.92. A volume a
  document states and its own table or dimensions times pieces bear out (`_document_volume`, `_table_volume`) wins
  over the mail's. A stated volume nothing in the document confirms does not. Live requests 2264 and 2537 had
  attachments that agreed with their mails. `rfq.evidence` records its source. A positive weight recovered by code clears
  the missing-weight flag. A document number alone is no pickup postcode.
- A number the model returns that the mail does not write is the model's own arithmetic ("480 lbs" to kg, dimensions
  to cbm): `_from_model` replaces it with code's reading of the parts, and keeps the model's only when code has none.
- A pickup "from our warehouse" can name no postcode and no town but the signature's (round 15): truckers then answer
  "To quote we need: the pickup postcode", and a quote without the pickup is too low. They price by zone: the
  request names the zone's first postcode (`pricing/bids.py` `_postcode`) when the mail has none.
- A request handled late (round 17: 75 s after receipt, after a restart) asked the truckers 24 s before
  `target_at`; they answer about 30 s after the ask, so the quote went out without the pickup and was too low. The
  bid wait counts from the ask (`PIXEL_BID_WAIT_S`), not only from receipt.
- "No pickup needed" contains the pickup word: it is no door pickup. "No pickup before 9:00" is a time window,
  not a veto.
- A guessed origin or destination can be a nearby airfield that no carrier serves. `agent.py` `served_airports`
  can ask the model once about an unresolved code within the airport-check time budget. If sourcing finishes
  without a complete price, `core/orchestrator.py` `_first_price` checks for late options until `hard_at`
  in the live mailbox. Offline KPI replay advances its clock through this wait and can deliver captured bids.
- "Ready for pickup" also stands in mails whose customer delivers to the airport ("we will deliver it to MUC
  ourselves"): that is no door pickup.
- A lot's figure without "each" is for the whole lot ("9 pieces, 345 kg gross" is 345 kg). A lot line
  code cannot read leaves the whole list to the model: a sum without it would be short again.
- A figure per piece also stands outside lot lists ("4 cartons, each 48 kg", "250 kg per pallet", "3 Kartons à
  30 kg") and piece groups come as bullets: the rules multiply by the pieces or give the mail to the model.
- A volume the rules cannot settle (two sets of dimensions, no total) sends the mail to the model: a volume of 0
  under-prices a light, bulky shipment.
- "Not urgent", "no rush" and "price matters more than speed" contain the urgency words: they mean cheapest. A
  superlative the customer wrote (fastest, cheapest) decides; without one, a hurry they describe ("as quickly as
  possible", "time-critical", "speed matters more") means fastest. "Express" counts only as a service, not in a
  company's name. A negated superlative ("not the fastest", "muss nicht das schnellste sein") is no wish, and
  "quickly" alone is about our reply, not the flight.
- A request can come as labelled bullets ("- Destination airport: Toronto (YYZ), CA"): a label may carry the word
  airport before its colon.
- The model gave "de" for an English mail full of German place names: the mail's language is code's reading.
- A request's attachment (packing list, letter of instruction) can declare dangerous goods while the mail says
  "non-dangerous". `extract.py` `dangerous_goods` reads the mail and every attachment; a declaration adds
  `dangerous_goods_documents` to `Rfq.missing` and emits `rfq.dangerous_goods` with the declared text. "Non-dangerous
  goods" and "no dangerous goods" are no declaration. The quote
  must name the goods and ask for the DGD and the MSDS (round 11 was accepted that way, at the normal price).
- A request in German writes `1.250 kg` for 1250 kg and `12,5 kg` for 12.5 kg.
- Two weights in one mail (per piece and total) make the rules unsure: the model reads it.
- The prose slots must not contain a price or a code: the scorer reads the one total we write.
- The total is written like the portal's and the carriers' own: no thousands separator, a decimal point, also in a
  German mail. A grouped total can be read as a different number.
- Every step of the prose fails soft: an odd model answer, a missing name, a broken stored event or a rendering
  error each give the plain mail, never an exception on the quote or the fallback path.
- A reasoning model spends its output tokens on reasoning and can return empty slots: set `PIXEL_MODEL_PROSE` to a
  fast non-reasoning model.
- A labelled field reads the other way round from a sentence: "Service: express", not "express service". A number
  can use a space for thousands ("1 250 kg"). `tests/fixtures/rfqs/rewrites.jsonl` holds such phrasings; a row with
  `known_gap` is one the rules read wrong today.
- A carrier's answer can name the offer's validity in days next to the transit time ("valid for 7 days, transit 3
  days"); the first number of days is not always the transit. `tests/fixtures/mails/bid_probes.jsonl` holds such
  answers.
- The rules' preference and currency are final: no model checks them when the rules read a request.
  `tests/fixtures/request-variants.json` writes one request many ways (labels, a table, German, French, units,
  separators); each must be read right or go to the model. Its `hole` entries are wordings misread today, as
  strict xfails: speed without a superlative or "urgent" ("next flight out", "shortest transit",
  "time-sensitive", "AOG", "Express please") reads as cheapest; a currency in words, as a symbol, as "Price:
  USD" or beside a second currency reads as none and is quoted in EUR.

## Open

- The quote is in the chosen rate's currency even when the customer asked for another (Q7).
