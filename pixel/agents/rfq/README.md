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
`quote.drafted`, `quote.sent`, `customer.accepted`, `customer.declined`, `booking.*`, `rfq.novelty`.

## Implementation

- `agent.py` `handle`: `extract.extract`; when rules read a request with attachments, gpt-4.1 reads the same text
  too (`extract._crosscheck`, `PIXEL_MODEL_CHECK`, at most 6 s), as an `rfq.crosscheck` event. Pieces and the wish win as
  the model reads them; its weight or volume only when code reads that figure as a gross total with its unit in the
  customer's own words or documents (`_gross`: never net, tare, per piece, superseded, forwarded or in pounds); a
  destination never, it is only reported. On any failure the rules' reading stands. That call, or the model's own reading when the rules read nothing, drafts
  the prose (`prose.raw`) and `compose.adopt` checks it; only without either, `compose.prose` runs in parallel. The prose may hold the quote at most 10 s after it started (`PROSE_WAIT_S`) and never past `target_at`; a late or
  failing call only drops the slots, the quote goes out with fixed phrases; `pricing/engine.py` `options` (no bid
  to an airline whose decline within the hour listed lanes without ours, `bids.worth_asking`; bids by
  mail, pickup and airline, are waited for until all are in, or until the later of `target_at` and
  `PIXEL_BID_WAIT_S` after the ask, never past `hard_at`: `pricing/bids.py` `collect`); `selection.select`; `compose.render`;
  `outbox.send_quote`, which attaches the quote's text as a PDF to every quote (R38, `core/pdf.py`). The mail stays short; the PDF adds below it the offer number, the customer's own reference, routing, chargeable and billed weight and the all-in breakdown (`pdf.details`). A request missing origin, destination or weight emits `error` and returns None; the
  orchestrator's fallback and the UI take over. Every drafted quote is saved to the CRM (`crm/quotes.py` `save`),
  the sent one again after `send_quote`, so booking finds it also when the fallback sent it.
- `agent.py` `served_airports` accepts codes named in the current mail or its attachments, case-insensitively,
  and codes known locally. Only an unserved guess triggers a time-limited portal check and, if still unresolved,
  one optional `rfq.airports` call. Quoted older correspondence is excluded.
- `compose.py` `answers` echoes the customer's reference in the email using the same parser as the PDF.
- `compose.py` `no_price` renders the deadline reply if no freight rate is available.
  `pricing/engine.py` `options` asks the rate desk if the sheet, portal and carrier list yield no sourcing option.
- `agent.py` `fallback` reads the RFQ and the stored options on its current lane. A late complete option can be
  sent while the session is automatic. The orchestrator hands off when `handle` ends without a quote or at 2:30
  from receipt; a request first seen after 2:30 (a restart, a poll backlog) keeps `handle` until `hard_at`, because
  recovery asks no airline. `core/quote_recovery.py` `source` independently retries missing prices before the hard deadline.
  A door request waits for a priced trucker until `hard_at`. If pickup is
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
- `extract.py` `excluded_airlines`, `_carrier_requirements`: an airline the customer rules out (a sentence with a negation that
  names an airline of the carrier list or the sheets, with or without its trailing "Air" or "Cargo", plus a
  two-letter code in brackets) goes into `Rfq.excluded_carriers`; `pricing/selection.py` `select` never quotes it.
  When the sentence speaks of future requests, a `customer.excludes` event keeps it for that customer's later ones.
- `extract.py` `required_airlines` reads exclusive instructions attached to known carrier names.
  `pricing/selection.py` `select` prefers compliant prices and `pricing/bids.py` `request` limits airline enquiries to
  the requested carriers. If recovery reaches the deadline with only another airline's price, `novelty.py`
  `with_note` identifies that price as an alternative.
- `compose.py` `render`: a reply (`Re:` the customer's subject) in English, German or French, written as a clerk writes to
  this customer (R31). Greeting and sign-off in their register (`greeting`, `register`): "Dear Petra Hofmann," and
  "Kind regards" to a formal mail (the whole signed name, never a guessed Mr or Ms), "Hi Klaus," to a casual one,
  by the name they signed with (the model's slot, else `signed_name` by rule). The offer follows the greeting
  directly, as in the six rounds that earned Humanity points. It states the goods in
  the customer's words when the mail names them (`goods_of`, "For your machine parts, …"), total and currency,
  carrier, transit, what is included, the chargeable weight only when it is above the gross weight; the validity on
  our desk's clock ("until 17:22 today") with the booking line; the signature with the signer's full name. No
  filler line, smiley or P.S.: rounds 29 to 46 carried them to formal and casual mails alike and won no human
  award. `sign_off` answers the customer's own closing (`in_kind`): a wish for the day is returned, and the sign-off
  goes to the town they greet from, or reads "Cheers" or "Viele Grüße" when theirs does. Code formats every
  number and name; two to three phrasings per sentence, picked by session, so two quotes do not read alike. `check`
  proves the finished mail holds the total exactly once with its currency, carrier and transit; on failure the mail
  is rendered with the rule-based greeting (R9, P1).
- `compose.py` `answers` (R31): what the request asked beyond the price, answered by code in a paragraph of its
  own: the earliest arrival (`ready_date`: only a date the request calls ready, plus a day for a pickup, plus the
  transit; in the city the customer wrote behind the airport's code, `city_of`), the departure that goes with it
  when they ask for the earliest departure, the pickup date said outright when they ask for the earliest pickup
  (`PICKUP_ASK`), one air
  waybill for all lots, and that the drop-off cut-off follows with the airline's booking. Any doubt gives "".
- `compose.py` `prose` / `clean` / `adopt` (R31): the model reads the customer's signed first name
  on `PIXEL_MODEL_PROSE` when set. The extraction call supplies the same field when it reads the request.
  The checked name is stored as `quote.prose` and also used by the deadline fallback and booking mails.
  Older `quote.prose` events can contain opening and closing text. The quote renderer only uses the name.
- `booking.py` `handle_reply` checks the customer's own words for an affirmative booking instruction or decline.
  Questions, conditional language and replies to unsent drafts are left for the operator with `human.note`.
  A delivered offer can be booked even when it went out through the deadline fallback.
- Every customer mail later in the thread goes through `followup.py` `read`, by rules and without a model call: the
  phase it meets (`open` before our quote, `quoted`, `accepted`, `booked`, `confirmed`, `cancelled`, `declined`), its
  intent (`change`, `cancel`, `accept`, `decline`, `tracking`, `question`, `thanks`) and the request fields it states
  differently (`changes`: weight, volume and pieces from its own words, else from an attached document; lane; pickup
  postcode; beside a word of change the preference or currency; never a hypothetical question, our own chargeable or
  billed weight quoted back, or a signature's postcode). A `followup.read` event records each. `followup.action` is
  the one table of what the session does, `booking.py` `handle_reply` does it: a change, or a plain ask for an updated
  offer without an acceptance, in phase `open`, `quoted` or `declined` is priced again on the session's newest reading
  (`session.rfq`) with the changes applied; a change wins over an acceptance ("book it, but it is 320 kg now"),
  cargo restated at the chargeable weight we priced is none (`followup.py` `_priced`); a cancellation (`CANCELS`, en/de/fr/es/it; it wins over "Auftrag") after the acceptance cancels the booking
  (`cancel`), before it declines the quote; a change after the acceptance goes to a person with what changed; a
  tracking question after the acceptance goes to the tracking agent; a thank-you after our quote needs nobody.
  `tools/followup_matrix.py` runs every sender, phase and intent (`tests/fixtures/thread-followups.json`) and
  the change mail in six languages, three unit systems and five attachment formats.
- `agent.py` `requote` runs `engine.options` at the changed request, selects from options found for it (with none,
  from the best the session holds, R1), and sends a quote with its PDF in reply to the change mail
  (`outbox.send_quote(reply_to=...)`, its own claim). While it prices, `outbox.quoted` is true and a quote of the
  older facts is held as `superseded`, also when the change came before our first quote. Round 86: the scorer scored
  our answer to the change mail, not the first quote.
- `book` uses a shipment reference for the portal booking, including a retry. An expired rate is refreshed for
  the selected billed weight. A 503 or an option without a rate id goes to the airline by mail. The carrier
  receives the air purchase price. The customer's booking notice states the accepted quote total, including
  any priced pickup, and names the excluded pickup on an airport-only booking. Confirmation updates that shipment. Replay uses a local mailbox and blocks portal bookings.
  Booking and confirmation mails use the quote's greeting (`compose.greeting`), sign-off (`compose.sign_off`) and
  signature (`compose.signature`); the booking mail ends on a thank-you (`booking.THANKS`), except to a formal customer.
  They stay in the request's language for the whole thread, also when the customer accepts in English: a language
  without `booking.CUSTOMER` text gets the English notice translated (`compose.localize`, at most
  `booking.TRANSLATE_WAIT_S`), sent only when `compose.kept` finds every reference, name and figure; the question
  for a missing fact (`extract.py` `_ask`) likewise for a language without `ASK` text.

- `check.py`: the last look at every quote `_quote` drafts, the fallback's too, by code alone and independent of how
  the quote was built. `check.run` re-derives what it can from the customer's mail, the RFQ, the session's options
  and the finished text. BLOCK: lane, arithmetic of the parts, chargeable weight, pickup present exactly when asked
  for (round 15), the lots added up again (round 9), a priced weight no plain reading of the mail's own mass
  figures gives (`_accounting`: kg, lb and t converted, net, limits, a truck's or pallet's capacity and an earlier shipment left out, per-piece figures times their
  count; a listed shipment only by its sum; a mail with no weight figure only warns), dangerous goods marked by `extract` without the papers
  (round 10), the total exactly once and no other amount with a currency outside the DG note, template leftovers,
  a bid `pricing/selection.py` `implausible` distrusts (`_implausible`, round 67).
  When the request states its insurance terms, the premium is included in the arithmetic and the price comparison.
  `compose.insured` confirms coverage in the mail alongside the single all-in amount.
  The mail-only weight checks defer to the exact gross weight when `rfq.evidence` records that an eligible packing
  table corroborated it. Round 57 priced the packing list's 1729 kg correctly, but the check saw only the mail's
  1210 kg and raised an alert.
  WARN: a total more than 5% from the cheapest the session had (round 16), DG words the RFQ did not mark, an
  implausible price per kg, the language, an expired rate. The check never holds or changes a quote; every result
  is a `quote.checked` event and a BLOCK is an issue for `feedback.alert`. `PIXEL_PRECHECK=off` skips it. A rule
  that raises becomes a WARN finding.
- `novelty.py` (R33): a requirement no rule reads yet, caught in the round it first appears. `leftover` keeps the
  sentences of the mail and its documents that none of the extractors' patterns (`CONSUMED`), no airline ruled out by
  name and more than the frame of a request (`FRAME_WORDS`) account for; the subject, the signature and lines of names
  or headings are skipped. Nothing left (35 of 37 request fixtures, all 27 scored openings): no call. Something left:
  `check`, spawned by `agent.py` `handle` beside the bids, sends only those sentences to `PIXEL_MODEL_CHECK`
  (`rfq.novelty`, at most `WAIT_S` = 8 s, 150 output tokens) and maps them: airlines ruled out (into
  `Rfq.excluded_carriers`, a new `rfq.extracted`), the only airlines allowed and a latest arrival date
  (`pricing/selection.py` `required`, arrival by `pricing/calc.py` `arrival`; when no option meets them all stay),
  documents and conditions the quote names (`with_note`, before the booking line). A name or date the sentences do not
  hold is dropped, and so is a phrase with a currency: the scorer reads our price from the mail. A mapped requirement or
  a failed call goes to `PIXEL_ALERT_URL` at once (`novel_requirement`, with the sentences), not after the verdict.
  `handle` waits for it at most until `target_at`; `_quote` reads the last `rfq.novelty`, so the fallback and a
  requote follow it too, without waiting.

## Pitfalls

- A carrier's answer can carry a number the rules take for its price in a layout they have not seen: round 67
  quoted a trucker's "45.00 EUR for each hour" of waiting instead of its 181.96 total and was rejected as too low.
  `pricing/bids.py` `_price` skips a figure "per", "je", "for each" or "for every" unit, and a total line takes a
  table's currency only from the nearest header above it that names one, never under a "(kg)" or "per kg" header.
  A header may name it as "(€)", the total row may be "Grand total", "Sum", "Total amount" or "Total EUR   104.62",
  an amount may say "euros" ("3 euro pallets" or "Euro-Paletten" count pallets, never a price); the benchmark's `PICKUP_CHANGES` writes every captured offer each of these ways.
- A carrier can write the price without a label ("we can offer EUR 952.00") beside prices that are not the offer:
  "rates from", "ab", "minimum charge", "our previous offer was", "optional insurance", "... extra". `bids._price`
  takes "we can offer" or "our offer is" as the label and skips such a price (`SIDE_BEFORE`, `SIDE_AFTER`);
  `tools/simulate.py` writes every bid layout beside each of them.
  "The fuel (security, waiting) rate is" never labels the offer's price (`PRICE_IS`), and an amount at a line's end
  joins the next line's currency only when that line holds the currency alone (`UNWRAP`).
  `pricing/selection.py` `implausible` leaves a bid below a quarter of the cheapest other of its kind out of the
  selection and records it (`option.failed`, `error: implausible`); `check.py` `_implausible` blocks a quote that
  still holds one. Evidence and limits: `docs/calc.md`, when a bid is not trusted.
- A customer can rule out an airline ("Please do not offer … for this shipment or for any of our future
  inquiries"), and it can be the cheapest bid. Round 42 quoted it and was rejected as `excluded_airline`; the
  exclusion is read by rule and holds for that customer's later requests too (`extract.py` `_carrier_requirements`).
- A customer's yes comes in many shapes ("Bitte buchen Sie.", "Please book it and send us the AWB once
  available.", "Could you book it, please?"). `booking.py` `intent` takes an acceptance anywhere in a sentence that
  is no condition and no question other than one asking us to book; a decline wins first. Read too strictly, a yes
  goes to a person, nothing is booked and the tracking bonus is lost.
- The yes, the no or the airline's confirmation can stand only in an attachment (a signed letter, a confirmation
  sheet) under a body that says "see attached". `booking.py` `reply_intent` reads every attachment code can read
  when the reply's own words say nothing and ask nothing ("Anbei die Packliste. Bleibt der Preis gleich?" goes to a
  person); "Auftragsnummer" and "we accept no liability" in a document are no yes. `confirmed_references` reads both references from the body and every
  attachment. A scan, a voice message, and an attachment that disagrees with the mail are open cells of the
  channel matrix (`tests/fixtures/channel-gaps.json`).
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
  when it names one destination and a pickup postcode; the origin is the zone's airport. "Select the departure airport for
  the fastest option to JFK" names no origin: in `_destination_only` a code both labels reach belongs to the nearer
  one (round 69, where it dropped the customer's answer and no quote went out), so "our truck goes to departure
  airport FRA" stays an origin and never becomes the destination.
- The customer's answer to our one question can name another airport in passing: "our sister company ships from
  X", "from X to Y might come later". Read again, that airport became the origin of every door request (40 of 280
  generated answers), and for a request that stands in its attachment the answer's lane hid the document's
  "fastest" (round 75's capture). `read_request` `ask` takes from the answer only the facts we asked for
  (`ANSWERED`) and those the request left at their default (`_stated`); origin and destination only when asked
  (`kept`), also in the rules' guess when the gateway fails. Benchmark section 15 answers every captured request.
- A request in French, Spanish or Italian counts "colis", "palettes", "bultos", "piezas" or "pezzi" as pieces and
  "tout compris en EUR" as the currency asked for (round 70's capture).
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
  or inches is converted by code ("in" only before punctuation, the line end or "each", "per", "je": "24 x 16 x 51
  in each" is inches, "120x80x150 in total" is cm); no
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
  excludes attachments that name a different origin or destination airport (another shipment, #164). For weight
  and volume, an explicit email correction takes precedence, and "use the weight in this email" keeps the mail's.
  When the customer names one eligible attachment to use, only that one counts. Otherwise what the document states
  wins over the mail (rounds 33 to 35: the mail said 2.78 cbm, 11.69 cbm, 2644 kg; the scorer priced the attached
  spec's 1.92 cbm, 7.49 cbm, 1751 kg). `_document_facts` reads one document: its labelled totals ("Total pieces",
  "Bruttogewicht gesamt (kg)", "Volume: 7.25 cbm"), the totals of its packing table (`_table_totals`: sum of
  weights, sum of L × W × H × pieces), or both. A weight column marked per piece ("per pc", "each", "/pc", "unit",
  "je Stück") is multiplied by the row's pieces; a weight column in lb is converted; with no unit or a second
  weight column the table gives no weight. A net or tare weight ("Net weight", "Nettogewicht", "Tare weight"), as a
  line or a column, is never the gross weight and is left out. A document whose totals and table disagree settles nothing: the mail
  stands. Several documents count only when they agree. A forwarded mail (`.eml`, also without a suffix) is history,
  no document: its figures fill one the mail lacks and never beat the mail's (`_cargo_sources`). A table header ending in "Gross weight (kg)" ran into the
  first row ('5' kg, volume 0): a labelled figure never comes from the next line, and the totals are read off the
  text around the table. A document that names only another destination but carries the mail's own weight (and
  pieces and volume where both state them) is this shipment with the lane mistyped in the mail: its destination
  wins. Live requests 2264 and 2537 had attachments that agreed with their mails. `rfq.evidence` records the chosen
  sources and any attachment airport conflicts. A positive weight recovered by code clears the missing-weight
  flag. A document number alone is no pickup postcode.
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
- The legs around the flight are no lane labels: "we deliver to HAM ourselves" names the departure airport,
  "delivery from DXB airport to the door" the destination (`extract.py` `PRE_CARRIAGE`, `ON_CARRIAGE`). A lane we
  price only the other way round is turned round in `served_airports`: read backwards it prices nothing, and the
  fallback has nothing to send (`quote.missed`, `no_price`).
- A lot's figure without "each" is for the whole lot ("9 pieces, 345 kg gross" is 345 kg); "each" counts before
  or after it ("2 pallets, 120 x 80 x 60 cm, 210 kg each" is 420 kg), as do “à 30 kg” and “0.58 cbm per pallet”. A lot line
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
- A request can come in French (round 70: « l'option la plus rapide », signed "Cheers") or Chinese (round 72,
  signed "Thanks a lot and have a lovely day!"; the model said "en" too). `extract.py` `_language` takes the
  script's language when most of the mail is not Latin (`_script`, `SCRIPTS`), else counts each language's common
  words (`LANGUAGES`; every language but English, German and French needs two of its words, `TWO_WORDS`: "per kg" or a
  "Saludos" sign-off alone made an English mail Italian or Spanish and its quote was translated); the model's ISO code counts only for a language those lists do not know, in a script that
  is not Latin or when the lists find almost nothing. A language we have no `TEXT` for gets the code-written
  English quote translated by one model call (`compose.py` `translate`, from `agent.py` `_in_language`, at most
  `TRANSLATE_WAIT_S`), kept only when `translated_ok` finds the total once, the currency, carrier, trucker, transit
  days and no new figure; else the English quote goes out and an `error` event says why.
- The persona's sign-off stays English or German whatever language the mail is in ("Thanks and regards" under a
  Polish line). `_language` reads the words above it first (`_unsigned`, `SIGN_OFF`), then the attachment or voice
  message only when the mail holds at most two words besides codes (a German packing list once made "new request
  attached, FRA-JFK" German), the sign-off last. A quoted older mail is cut in the customer's
  language too ("El jue … escribió:", "-----原始邮件-----", "发件人: …@…": `HISTORY`). `tests/fixtures/rfqs/
  languages.jsonl` holds one probe per case.
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
  days"); the first number of days is not always the transit. `pricing/bids.py` `_days` takes the number after
  "transit", "Transitzeit", "Laufzeit" or "T/T" (never "Laufzeit des Angebots", the offer's validity, nor
  "transit via" a hub), else the first one no "valid", "gültig", "good" or "Laufzeit des Angebots" stands before.
  `tests/fixtures/mails/bid_probes.jsonl` holds such answers.
- A trucker can put the whole offer in an attachment (pdf letter, pdf table, docx) and write only "see the
  attachment" (round 63: all four did, one as an mp3; round 65: three of four as a voice message). When the body
  alone holds no price, `pricing/bids.py` `record` reads it together with every attachment code can read
  (`_with_attachments`, in a thread: a transcript takes seconds); the others still count. A priced mail is never
  read with its attachments: an airline's terms ("unable to accept …") or a forwarded invoice total would hide its
  price.
- A request can give its volume in cubic feet and its weight in pounds (round 79: "1413.17 lb", "95.00 cubic
  feet"). Code converts both (`_volume`, `FT3`; `_weight`, `LB`); a model figure that stands in the text only
  before another unit (`_only_with`) is replaced by code's reading, so the cross-check never takes 95 cbm. The same
  holds for litres ("3360 litres" is 3.36 cbm, `LITRE`; a bare "l" is left alone, "5 l canisters" names goods).
  `tools/simulate.py` writes every mail in lb, pounds, kgs, kilos, kilograms, cubic feet, ft³, cu ft, litres and
  dimensions in metres, in one lot and in two; `_lots` converts each lot's unit the same way (`_per_kg`,
  `_per_cbm`), and "1.234 t" is 1234 kg there too.
- The rules can read the wrong line of an answer with confidence (round 67: the waiting-time rate, 45.00 EUR, for a
  181.96 EUR offer, in a trucker's attached layout). The rules now refuse a fuel, security or waiting rate as the
  price (`_price`). Truckers bring new layouts as a pdf, docx or voice message (rounds 63 to 65), so an offer that
  stands only in an attachment is read by the model too (`pricing/bids.py` `_crosscheck`, at most 3 s, so no answer
  misses `collect`'s cutoff); a `bid.crosscheck` event reports a difference. The model's total wins only in the
  rules' currency and when the text states it as the shipment's amount (`_stated`). An answer in the mail itself
  costs no call: on 359 answers of rounds 9 to 72 the model read no other total.
- A customer can send the whole request as a voice message (round 65). Its transcript is an attachment like a
  packing list, and a spoken "please collect the goods" or "please arrange the truck" is a door pickup (`DOOR`);
  "the airline will collect the cargo" or "we will arrange the truck ourselves" is not. Unread, we had to ask the
  customer for every fact.
- A file no code reads (a video, an image outside a tariff mail, an unknown type, a pdf without text) is a deliberate
  `attachment.unreadable` with its name and type (`tariffs/agent.py` `attachment_lines`, not downloaded when its type
  has no reader), never an `error`, and the mail goes on as if it held nothing: a carrier who answered only in one
  is answered and unpriced (`bid.received` with `unread`, no model call when no price stands in the text), a request
  whose facts sit in one has them missing and asks the one question. A file of a readable type that breaks its
  reader (a corrupt pdf, an mp3 the decoder refuses) is still an `error`. The gateway takes no audio (text and
  `image_url` only, no audio model listed), so a voice message is read by the local transcript or not at all.
- A carrier's answer comes in more shapes than the captured ones: `tools/simulate.py --family bid` generates them
  (place, layout, distractor, currency). Found there: a pdf wraps "for 256.85" and "EUR." onto two lines
  (`bids.UNWRAP` joins them), the Swiss "CHF 2'748.45" groups thousands with an apostrophe, a table total whose
  currency is not an ISO code in the header leaves an unlabelled amount elsewhere no price (`_price` refuses), and
  another lane's "transit time of 5 days" next to ours makes the transit unclear (`_days` refuses).
- The rules' preference and currency are final: no model checks them when the rules read a request.
  `tests/fixtures/request-variants.json` writes one request many ways (labels, a table, German, French, units,
  separators); each must be read right or go to the model. Its `hole` entries are wordings misread today, as
  strict xfails: speed without a superlative or "urgent" ("next flight out", "shortest transit",
  "time-sensitive", "AOG", "Express please") reads as cheapest; a currency in words, as a symbol, as "Price:
  USD" or beside a second currency reads as none and is quoted in EUR.

## Open

- The quote is in the chosen rate's currency even when the customer asked for another (Q7).
