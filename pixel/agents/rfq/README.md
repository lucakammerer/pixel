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

- `agent.py` `handle`: `extract.extract`; when rules read the request, `compose.prose` runs in parallel as the
  one gateway call of the quote (R28); when the model read it, that call already drafted the prose (`prose.raw`)
  and `compose.adopt` checks it. The prose may hold the quote at most 10 s after it started (`PROSE_WAIT_S`) and never past `target_at`; a late or
  failing call only drops the slots, the quote goes out with fixed phrases; `pricing/engine.py` `options`; `selection.select`; `compose.render`;
  `outbox.send_quote`. A request missing origin, destination or weight emits `error` and returns None; the
  orchestrator's fallback and the UI take over. Every drafted quote is saved to the CRM (`crm/quotes.py` `save`),
  the sent one again after `send_quote`, so booking finds it also when the fallback sent it.
- `agent.py` `fallback`: the RFQ and every `option.found` of the session, read back from the event log
  (`pricing/session.py`), combined and selected by code alone, no model call. Returns the quote unsent.
- `extract.py` `by_rules`: lane (`AAA-BBB`, `AAA to BBB`, `City (AAA) to City (BBB)`, `Origin:`/`Destination:`),
  weight, volume (or dimensions × pieces), pieces, fastest wording, door pickup with a 5-digit postcode, an explicitly
  requested currency, `de` or `en`. None unless exactly one lane (for a door pickup with a postcode: one destination)
  and one weight are found.
- `extract.py` `extract`: `by_rules`, else exactly one `llm.ask_json` (`rfq.extract`); code validates the answer and
  lists what is missing in `Rfq.missing`. A request without an origin airport but with a pickup postcode takes the
  airport of the pickup zone as its origin (R16), and so does a door-to-port request whose origin is an airport no
  pickup zone trucks to; when the zone has none, the model reads the mail. Emits `rfq.extracted` with the whole `Rfq`.
- `compose.py` `render`: a reply (`Re:` the customer's subject) in English or German, written as prose the way a
  clerk would: greeting in the customer's register (`register`: formal "Dear", neutral "Hello", casual "Hi") with
  the first name they signed with (the model's slot, else `signed_name` by rule), one short opening line, one offer
  paragraph with the all-in total and its currency, carrier, transit, what is included, the chargeable weight only
  when it is above the gross weight, and the validity on our desk's clock ("until 17:22 today"), then the booking
  sentence and a person's signature (`PIXEL_SIGNER`) with the matching sign-off. The mail ends there: no closing
  line. Code formats every number and name; three to four phrasings per sentence, picked by session, so two quotes
  do not read alike. `check` proves the finished mail holds the total exactly once with its currency, carrier and
  transit; on failure the mail is rendered without the model's slots (R9, P1).
- `compose.py` `answers` (R31): what the request asked beyond the price, answered by code in a paragraph of its
  own: the earliest arrival (`ready_date`: only a date the request calls ready, plus a day for a pickup, plus the transit), one air
  waybill for all lots, and that the drop-off cut-off follows with the airline's booking. Any doubt gives "".
- `compose.py` `prose` / `clean` / `adopt` (R31): the model writes three slots, `first_name`, `intro` and
  `closing`, on `PIXEL_MODEL_PROSE` when set. The intro is one short sentence; `clean` drops it when it repeats the mail back ("I've noted", "the details you
  provided") or runs past one sentence. The closing slot is no longer rendered. `clean` drops each slot that holds a digit, a three-letter code, a
  currency symbol, a URL, a weekday or month, a carrier's name, a greeting or sign-off, the wrong language, German
  `du`, or is too long; the first name must stand in the customer's mail. The checked slots are stored as
  `quote.prose`, so the fallback quote and the booking mails use them too.
- `booking.py` `handle_reply`: `intent` reads accept, decline or other by rules (English and German, a decline
  wins); anything else becomes a `human.note` for a person, no model call. `book`: one shipment (`PX-…`, our
  `team_reference`), one portal booking by `rate_id`; a lost connection is retried once (the portal answers 200 with
  the same booking), 410 fetches a fresh rate and books that, 503 or a quote without `rate_id` books by mail with the
  airline (address from the 503 message, else the carrier list), 404 and 409 are `booking.failed`. A second
  acceptance books nothing twice. A dry run (`PIXEL_DRY_RUN=1`) and every replay never book at the portal: the
  booking falls through to the booking mail, which the outbox holds. The customer mails (booked, confirmed) are in the customer's language, with
  money and weight formatted by `compose`, the stored first name and the same signer, without a model call.
  `handle_confirmation`: "Booking <reference> confirmed" marks booking and shipment
  confirmed and tells the customer once.

## Pitfalls

- A request can sit above a forwarded or quoted older mail with its own greeting, name and dates: the reply's
  name, register and answers come from the customer's own words only (`extract.py` `own_words`, the same cut pricing reads).
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
- A pickup can be named by its town and no postcode ("from our warehouse in Augsburg"): `extract.py` `_town_zone`
  takes the zone whose area the mail names, only when it names exactly one (a signature can name another town).
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
- A request can leave its weight to the attached packing list ("Total gross weight (kg): 715"): when the mail names
  no weight, `extract` reads the attachments' text with it, for the rules and the model. A document number ("No.
  53839") is no postcode.
- "No pickup needed" contains the pickup word: it is no door pickup. "No pickup before 9:00" is a time window,
  not a veto.
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

## Open

- The quote is in the chosen rate's currency even when the customer asked for another (Q7).
