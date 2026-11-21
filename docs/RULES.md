# Rules

Every rule we are scored or judged by, numbered. Refer to them as `R12` in code comments, tests, commits and docs.
Sources: **API** = the endpoint description in `https://api.start-hack.5u.ai/openapi.json`, **Brief** = challenge
brief, **Slides** = deep dive, **Kit** = the wording inside the tariff sheets, **Board** = `GET /v1/board`.
When sources disagree the API wins, then the brief. Status: `open` until code and a test exist, then `done`.

## Mailbox

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R1 | A new RFQ arrives every 6 minutes and expires 6 minutes later. A missed or rejected request is lost; there is no second chance | Brief | `core/orchestrator.py` `deadline_for`, `quote_with_deadline` (recovery at 2:30), `_first_price`, `recover`; `core/quote_recovery.py` `source` | done |
| R2 | Everything arrives in one mailbox: customer requests, carrier bids, rate desk answers, booking confirmations, tariff mails, tracking questions. Poll `GET /v1/mailbox/messages` with `since_id`, keep `next_since_id`; at most 200 mails per call; poll every 1 to 2 seconds. Each new mail is also pushed to the endpoint we subscribe (`PUT /v1/mailbox/webhook`): signed, answered with 2xx within 5 seconds, tried 3 times, possibly twice or out of order; a dropped push stays in the poll | API | `core/poller.py` `run`, `accept`, `core/webhook.py` `hook`, `core/orchestrator.py` `dispatch` and `recover` (stored handler completion) | done |
| R3 | `references` lists a thread's mail ids from the first mail to the parent. Attachments are listed on the mail and downloaded one by one | API | `api/mailbox.py` `to_message` (the sender's address without a display name), `attachment`; `core/poller.py` `session_of` | done |
| R4 | A quote counts only as a reply (`in_reply_to`) in a thread a customer started, and only our first quote in a thread counts. `in_reply_to` must be a mail we sent or received, else 404 | API, Brief | `core/outbox.py` `send_quote`, `repair_receipts`, `reconcile_reply` (one claim for operator, agent and fallback) | done |
| R5 | We may write to customers, the airlines and truckers of the carrier list, `ratedesk@hackloop.mail` and `tracking@hackloop.mail`. Any other address gets 422 | API | `core/outbox.py` `allowed` | done |
| R6 | Attachments: at most 5 per mail, 10 MB each; pdf, xlsx, xls, csv, txt, png, jpg, docx; seen live also mp3 voice messages | API | `api/mailbox.py` `check_attachments`; `agents/tariffs/agent.py` `_lines` (an mp3 by `_transcribe`) | done |
| R7 | 60 mails per minute per key, then 429. Limits may be tuned | API | `core/outbox.py` `_deliver` | done |
| R8 | Only mail that 5U's agents start is scored. Mail between teams changes nothing | Brief | — | n/a |

## The quote

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R9 | Every quote states one all-in total with its currency, for one option: the one the customer asked for, cheapest or fastest, never on an airline the customer ruled out (round 42: `excluded_airline`) | Brief, scored requests | `pricing/selection.py` `select`, `allowed`, `agents/rfq/extract.py` `excluded_airlines`, `_ruled_out` (also for the customer's later requests when they say so), `agents/rfq/compose.py` `render` (airport-to-airport price with pickup explicitly excluded if the deadline arrives without a trucker price) | done |
| R10 | Prices come from the rate sheets, the rate portal and bids from 2 to 5 carriers. Some airlines are only in the sheets, others only on the portal; destinations missing on the portal are priced by mail to the airline | Brief, API | `pricing/engine.py` `options`, `pricing/bids.py` `parse` (separate portal and carrier-mail sourcing, flat totals and per-kg bids, amounts a pdf wrapped or grouped by an apostrophe; transit after its label, never the offer's validity, none when two labelled transits differ; generated answers: `tools/simulate.py` family `bid`), `pricing/selection.py` `implausible` (a bid below a quarter of the cheapest other price of its kind is not used, round 67; `agents/rfq/check.py` `_implausible`) | done |
| R11 | Chargeable weight = max(weight in kg, volume in cbm × 167); the bracket is picked by the chargeable weight | Kit, API (`Rate.chargeable_weight_kg`) | `agents/rfq/extract.py` `read_request` (explicit cargo corrections and named attachments; airport conflicts excluded; the document's pieces, gross weight, volume and destination, `_document_facts`), `pricing/calc.py` `chargeable_kg`, `agents/rfq/check.py` `run` (gross weight checked against the packing weight the document gave when present) | done |
| R12 | Sheet price: rate per kg of the bracket × billed weight, at least the lane's minimum, which applies to the freight before surcharges; plus fuel and security per kg of the billed weight | Kit | `pricing/calc.py` `price_tariff`; a sheet in another currency at its stated rate, else the newest portal rate (`pricing/engine.py` `_fx_eur`) | done |
| R13 | The best price is not always the obvious one: read the sheets closely (breaks differ per airline, a higher break can be cheaper, neighbouring breaks can carry the same rate). The best price we are scored against bills the next break on the portal too when that is cheaper, and includes the cheapest pickup | Brief, Kit, Board (the points of an accepted quote) | `pricing/calc.py` `price_tariff`, `pricing/portal_rates.py` `_at_next_break`, `tests/test_calc.py`, `tests/test_portal.py` | done |
| R14 | A tariff has a currency and a validity (from, to); airlines send new tariffs by mail every month | Kit, Brief | `agents/tariffs/agent.py` `parse`, `_pdf_pages`, `read_with_model`, `check`, `pricing/store.py` `lookup` | done |
| R15 | Among carrier bids the right one is the most balanced: price and speed weigh 50/50. Truckers are carrier bids: their speed is the pickup time in hours; the customer's wish does not choose the trucker (`docs/calc.md`, which trucker the scorer takes). Airline bids are not balanced: the scorer compares them by the customer's wish (round 28) | Brief, scored requests | `pricing/selection.py` `balanced`, `pricing/engine.py` `combine` | done |
| R16 | A door-to-port request adds a trucker pickup: the pickup zone belongs to one airport (`pickup_zones.csv`), truckers serve zones (`carrier-list.csv`) and quote only when the request names a postcode | Brief, Kit, scored requests | `pricing/pickup.py` `options`, `pricing/bids.py` `_postcode`, `_with_attachments` (an offer sent as a pdf, docx or txt; a file no code reads is `attachment.unreadable`, the trucker answered and unpriced) and `_price` (an offer letter's "our price is", a mail's "our all-in rate ... is" or "deliver it ... for", a table's total with its currency in the header (also "(€)"; "Grand total", "Sum", "Total EUR"; "euros"), none when that header names no currency, never a waiting time "for each hour") and `collect` (bid window measured from the ask, capped at the hard deadline), `pricing/engine.py` `combine` (without a trucker bid `selection.implausible` distrusts), `agents/rfq/agent.py` `fallback` (pickup explicitly excluded if still unpriced at the deadline) | done |
| R17 | No margin is added: margins are out of scope, the score compares our total with the best price | Brief | `pricing/calc.py` | done |

## The rate portal

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R18 | `GET /v1/portal/ports` lists every lane the portal prices with its airlines. `GET /v1/portal/brackets` gives an airline's weight breaks; an airline not on the portal returns 404 with the address to mail. Neither is rate limited | API | `api/portal.py` `lanes`, `brackets`, `pricing/portal_rates.py` `lanes` and `_at_next_break` (brackets cached by dataset version and airline) | done |
| R19 | `GET /v1/portal/rates` prices the weight we ask for, in that weight's bracket. Rate reads are capped per key in a fixed one-minute window: 429 with `Retry-After`; the limit is in `X-RateLimit-Limit`, what is left in `X-RateLimit-Remaining`. 503 comes with a message saying where to get the rate instead | API | `api/portal.py` `rate` (503 is `RatesUnavailable`: the API is down, not the airline off the portal), `pricing/portal_rates.py` `options` (quota rechecked between batches, actual-weight rates before higher breaks) | done |
| R20 | A portal rate's `total` is freight + fuel + security in `currency`; when the currency is not EUR the rate carries its exchange rate (EUR per unit). It carries `transit_days`, `valid_until` (15 minutes) and `dataset_version` | API, Brief | `api/portal.py` `_option`, `pricing/calc.py` `to_eur` | done |
| R21 | Rates can also come from a browser page behind a bot challenge and from the desk at `ratedesk@hackloop.mail`. The desk answers a request for a lane within about a minute, in a thread of its own that names our mail by its subject: the portal's rate per airline on the lane, each with a rate id, and the address of every airline that is not on the portal | Brief, observed | `pricing/engine.py` `_ask_desk` (one request per request, only after a rate read answered 503, mailed with the airlines and collected like their bids), `pricing/bids.py` `parse_desk`, `record` (desk answers by rules only, never the model); `core/poller.py` `session_of`; browser: not planned | done |

## Booking and tracking

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R22 | When the customer accepts, book by `rate_id` with `POST /v1/portal/bookings` and our CRM id as `team_reference`. One booking per `rate_id`. 201 booked; 200 same booking again (retry); 404 unknown; 409 booked under another reference; 410 expired: fetch a fresh rate and book that; 503: book by mail with the airline as the message says | API | `agents/rfq/booking.py` `intent`, `reply_intent`, `handle_reply`, `book`, `pricing/portal_rates.py` `refresh` (explicit consent to a delivered quote, refresh at selected billed weight) | done |
| R23 | A mail "Booking <reference> confirmed" from the airline follows: the shipment is then booked. There is no status call; the booking lives in our CRM. A booking by mail has no airline reference until then: its confirmation is found by our reference, which it names; the airline's reply in our booking thread ("Booking reference: BK-…", "Status: booked") is a confirmation too, unless it refuses ("could not be confirmed", "rejected") | API, observed (round 27) | `agents/rfq/booking.py` `confirmed_references`, `handle_confirmation`, `agents/router/agent.py` `rules`, `crm/bookings.py` `by_team_reference`, `crm/shipments.py` | done |
| R24 | Keep the customer informed | Brief | `agents/rfq/booking.py` `_notify_booked`, `_tell_customer` (accepted total in the customer notice, air purchase price in the booking record) | done |
| R25 | Tracking questions come from `tracking@hackloop.mail` and are answered from our own CRM | Brief | `agents/tracking/agent.py` `question`, `match`, `described`, `handle`, `crm/shipments.py` `find` (one identified shipment) | done |

## Scoring

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R26 | A quote is accepted when it is within 5% of the best price (first reply only, within 6 minutes). Only accepted quotes score, up to 100 points each; the score is the sum over all requests | Brief, Slides | — | n/a |
| R27 | Price accuracy, 45 points: full at the best price, fewer the further above it | Brief, Board | `pricing/`, `tools/kpi.py` `measure` (offline delivery, price and scope checked against fixtures), `core/feedback.py` `once` (scorer verdicts) | open |
| R28 | LLM cost, 40 points: the gateway spend of each 18-minute window, shared over all quotes accepted in that window, also those answered without a call; full at no spend per quote, zero at 0.10 USD. A window without any gateway spend earns no cost points. Every USD costs 400 points however many quotes share it, and the scorer counts what a tool or another machine spends with our key. Cost points are added when the window is over | Brief, API, scoring update of 21.11. | `llm.py` `ask_json`, `spend_usd`, `agents/rfq/agent.py` `handle` (one call by default), `crm/quotes.py` `cost_estimates` | done |
| R29 | Response time, 15 points: full within 2 minutes of the request, zero at 6 minutes | Brief | `core/orchestrator.py` `deadline_for` | open |
| R30 | +10 for a correct tracking answer on a booked shipment | Brief, Board | `agents/tracking/agent.py` `answer`, `tests/test_tracking.py` | done |
| R31 | +10 from the jury for the accepted mail that reads most human | Brief, Board | `agents/rfq/compose.py` `render`, `register`, `greeting`, `sign_off`, `signature`, `in_kind`, `prose`, `clean`, `_opening_ok`; `agents/rfq/booking.py` `_tell_customer` | done |
| R32 | All model calls go through the gateway (`POST /v1/gateway/chat/completions`, `/responses`, `/embeddings`), OpenAI models only, with our own OpenAI key stored once at the gateway. At most 20 gateway calls in flight per team, then 429 `too_many_in_flight` | Brief, API | `llm.py`, `api/gateway.py`, `tools/set_openai_key.py` | done |
| R33 | After a few hours the setup changes about once an hour, unannounced. We learn of a change from failed quotes: `GET /v1/board/me` gives per request the verdict (`accepted`, `rejected`, `pending`, `open`, `no answer`, `voided`), a reason code and the points per kind | Brief, API | `core/feedback.py`, `tools/board_me.py` `run`, `crm/quotes.py` `stats`, `ui/dashboard.py` | done |
| R37 | A quote not in the language the customer wrote in is rejected (`wrong_language`), whatever its price. Requests come in any language with the persona's own English or German sign-off (French in round 70, Chinese in round 72); the thread keeps that language after the quote | Board (rounds 70, 72) | `agents/rfq/extract.py` `_language`, `_unsigned`, `_script`, `HISTORY`; `agents/rfq/compose.py` `translate`, `localize`, `kept`; `agents/rfq/booking.py` `_tell_customer`; `agents/rfq/extract.py` `_ask`; `agents/tracking/agent.py` `handle`; `tests/fixtures/rfqs/languages.jsonl` | done |
| R38 | A customer who asks for the offer as a PDF attachment rejects a quote without a PDF that holds the price (`no_pdf_offer`); the ask can sit anywhere in the request, in any language | Board (round 76) | `core/outbox.py` `_deliver` (every quote carries `offer-<quote id>.pdf`; a mailbox 4xx sends the quote without it), `core/pdf.py` `offer` (the mail's text; a script Helvetica cannot write keeps the figures) | done |

## The final

| # | Rule | Source | Lives in | Status |
| --- | --- | --- | --- | --- |
| R34 | The leaderboard alone does not pick the winner. Pitch: 3 minutes plus 2 minutes of questions. The jury judges the pitch, UX and design, the codebase and how human the mails read | Brief, Slides | `ui/`, the whole repo | open |
| R35 | Required parts: router; up to three agents (RFQ and booking, track and trace, tariff ingestion); pricing module with automatic ingestion of new tariffs; CRM with history, won and lost and a dashboard; sessions UI with streaming where a person finds a session and steps in | Brief | `agents/`, `pricing/`, `crm/` (`quotes.stats` for won, lost and the dashboard), `ui/routes.py` (sessions, live stream, take over), `ui/dashboard.py` | open |
| R36 | Scope: air export RFQs only, with a door-to-port option. Out: a TMS, margins, ocean freight, competition between teams | Brief | — | n/a |

## Our own rules

| # | Rule | Lives in |
| --- | --- | --- |
| P1 | Code does the arithmetic and the selection (Decimal, tested). A model only reads what code cannot: the customer's request, an odd carrier reply, an unknown tariff format | `pricing/calc.py`, `pricing/selection.py` |
| P2 | One small gateway call per quote by default: rules first, the model as fallback, and where the rules sufficed one minimal call for the quote's prose (R31), which also keeps every window above no spend (R28); a customer language we have no text for adds one call to translate the quote (`agents/rfq/compose.py` `translate`), and one per booking notice, question or tracking answer in that language (`compose.localize`). Tokens and cost are logged per call | `llm.py` |
| P3 | Always answer: every request has its own deadline; before it runs out we send the best price we have | `core/orchestrator.py` `quote_with_deadline` |
| P4 | Every mail in and out, every decision and every model call is an event. CRM and UI read that log | `core/events.py` |
| P5 | Nothing from one dataset version is hardcoded; tariffs carry a version and a validity | `pricing/store.py` |
| P6 | The repo is public. Keys live in `.env` only and are never printed, logged or committed | `config.py`, `api/client.py` |

## Where the sources disagree

- **Weights:** the slides say 55 accuracy, 30 cost, 15 time; the brief and `GET /v1/board` say 45, 40, 15. We build
  for 45/40/15.
- **Carrier list file name:** `carrier-list.csv` in `start-kit.zip`, `carriers.csv` on the kit page and in the
  second zip. The ingestion accepts both.
