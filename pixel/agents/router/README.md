# router

## Mission

Decide for every incoming mail which kind it is, so the orchestrator hands it to the right handler. Wrong routing
loses a request, so rules decide wherever they can and a model is asked only for the rest.

## Rules

R2, R3, R35.

## Input / output

In: a `Message` with its session. Out: a `MailKind`. Emits `route` with the kind and whether rules or a model
decided.

## Implementation

`agent.py` `classify`. Rules in this order: sender is the tracking address, the rate desk, a carrier of the carrier
list; the mail answers one of our bid requests or our quote; it carries a tariff attachment; it starts a new thread
from a customer. One `llm.ask_json` call only for a customer mail none of these place.

## Pitfalls

To be filled from the first real mails.

## Open

- Everything: this is the skeleton.
