# router

## Mission

Decide for every incoming mail which kind it is, so the orchestrator hands it to the right handler. Wrong routing
loses a request, so rules decide wherever they can and a model is asked only for the rest.

## Rules

R2, R3, R35.

## Input / output

In: a `Message` with its session (set by `core/poller.py` `session_of`). Out: a `MailKind`. Emits `route` with the
kind, `by` (`rules` or `model`) and the rule that decided.

## Implementation

`agent.py` `rules`, first match wins:

1. Sender is `tracking@hackloop.mail`: `tracking_question`. Sender is `ratedesk@hackloop.mail`: `ratedesk_reply`.
2. A mail in a customer thread from that thread's customer (`sessions.customer`): `customer_reply`; when it answers
   our question for a missing fact (`messages.purpose` `question`): `other`, the request's handler reads it.
3. Subject `Booking <reference> confirmed` from anyone else: `booking_confirmation`.
4. It answers one of our mails sent with purpose `bid_request` (`messages.purpose`, set by `core/outbox.py` `send`):
   `carrier_bid`.
5. It has an attachment of a tariff file type and comes from a carrier of the carrier list or starts a thread:
   `tariff`. From a carrier without one: `carrier_bid`.
6. It answers our quote: `customer_reply`.
7. A new thread that names a weight or volume, or two upper-case three-letter codes: `customer_rfq`.

`classify` asks `llm.ask_json` (purpose `route`, at most 20 output tokens) only when none of these match. When that
call fails, a new thread becomes `customer_rfq` (a missed request costs the whole request, R1) and anything else
`other`.

## Pitfalls

- A customer's request can carry an attachment (a packing specification as xlsx). A file alone does not make a
  tariff mail: only from a carrier of the carrier list, or when a mail that asks for nothing brings a file named
  like a tariff. Routing such a request to the tariff agent lost it.
- The carrier list lives in the `carriers` table that `pricing/store.py` fills; until the start kit is ingested,
  rule 5 still catches tariff mails by their attachment.
- Every captured request (`tests/fixtures/mails/rfq-*.json`) must be placed by the rules: `tests/test_router.py`
  checks each one. A request can give its weight in pounds or tonnes and its places as city names.
- Subject words are weak evidence: a customer can write "booking" and "confirmed" in a new request, and an airline
  can word its confirmation differently from the API's example. `tests/fixtures/mails/routing_probes.jsonl` holds
  such mails; `tools/benchmark_generalization.py` section 6 checks them.

## Open

- Replace the synthetic fixtures with captured mail and check every rule against them.
