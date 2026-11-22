# Voice calls

A person on a request's page calls a carrier whose bid is late or a customer who has not yet answered our question
for a missing fact. The call runs in the browser with an ElevenLabs agent; what the other side says reaches the thread
as an incoming mail would, and the same rules read it. Nothing calls on its own: a call starts only from the **Call**
panel, which shows only while an answer can still change the quote.

## Flow

1. `calls.targets` lists who may be called: a carrier with a `bid.requested` and no `bid.received` while
   `pricing/bids.py` `collect` still waits (until the target or the bid wait after our ask), and the customer while
   `agents/rfq/extract.py` `_ask` still waits for the answer to our question. Nobody once the quote is out (R4) or
   when the longest call would end within `START_MARGIN` of the hard deadline.
2. `calls.start` gets a signed URL from ElevenLabs and emits `call.started` with the target and what the agent asks
   for (`_wanted`). The key never leaves the server.
3. `call.js` runs the call with `@elevenlabs/client`. The agent's client tool (`record_carrier_offer` or
   `record_customer_details`) posts what it heard to `/api/calls/{id}/answer`.
4. `calls.answer` checks it (a positive amount, a three-letter currency, IATA codes, a postcode) and writes it in the
   words our rules read in a mailed answer (`calls.body`), then hands that mail to `core/poller.py` `accept`: the
   router places it by the mail of ours it answers. A carrier's offer becomes a bid through `bids.record`, a
   customer's fact is read by `_ask`. One answer per call. Its id is `VOICE_IDS` plus the call's id.
5. While the call is live (`calls.holding`, at most `CALL_MAX_S` from its start), `_ask` and `collect` keep waiting
   for its answer; `collect` never past the hard deadline. The fallback is untouched.
6. After the call `call.js` reports `call.ended` and fetches ElevenLabs' summary and transcript once
   (`call.transcript`).

The agent never names, compares or computes a price: it asks for the one all-in total the carrier states and repeats
it back. Its own language model runs at ElevenLabs, not at the 5U gateway, so a call adds no gateway spend (R28) and
is not logged by `llm.py`.

## Setup

In `.env`:

```sh
ELEVENLABS_API_KEY=sk_...   # permissions: ElevenAgents read + write
```

Then `uv run python -m tools.voice_agent` once: it creates the agent and its two client tools and adds
`ELEVENLABS_AGENT_ID` to `.env`. Run it again to push changes of `agent_config.py`. Restart the app.

To try it: `uv run python -m tools.voice_demo` and open http://localhost:8010/demo/new, a fresh demo request with a
late airline, a late trucker and an unanswered question (no mailbox, no gateway, nothing sent).

The browser needs a microphone and loads the ElevenLabs client from esm.sh; on another device the page must be served
over HTTPS.

## Pitfalls

- The 5U carriers and customers have no phone numbers: the person who answers the call in the browser plays them.
  Calls are for the demo and for a person stepping in; a scored round never depends on one.
- A call answer is stored like a mail, so `messages` holds rows with ids from `VOICE_IDS`. They have no copy in the
  5U mailbox: never reply to one by its id.
- The session page does not reload on new events while a call is live (`window.pixelCallLive`), or the call would end.
