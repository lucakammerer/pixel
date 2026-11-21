# Models

Which OpenAI model the gateway calls go to, and why. Measured with `uv run python -m tools.bench_models`: six request
mails written for the tool (German with dimensions and pickup, pounds, a terse one-liner, gross and net weight,
city names, one without the facts), the production extraction prompt and its limit of 200 output tokens, seven
fields per mail. Prices are OpenAI's list prices; "points" is what one call costs of the 40 cost points (R28).

## 21.11.2026, round 73: scored live requests

Is a larger model or reading every request by the model worth it? Measured on the live host: the 44 accepted
requests without attachments of rounds 1 to 72, whose final `rfq.extracted` the scorer bore out. Each read by the
rules, and by the production prompt through `extract._from_model`. Origin left out: for a door pickup code sets it
from the pickup zone after either reader. The run spent about 0.17 USD in one window.

| Reader | Requests fully right | Tokens in / out | Seconds | USD per call (list price) | Language field right |
| --- | --- | --- | --- | --- | --- |
| rules | 42/44 | - | 0 | 0 | (code) |
| gpt-4.1-mini | 42/44 | 723 / 137 | 1.4 | 0.0005 | 22/44 |
| gpt-4.1 | 42/44 | 723 / 120 | 1.2 | 0.0024 | 43/44 |

The same two requests failed for all three (round 3 pieces, round 19 postcode). On the request shapes we have seen
the model reads no better than the rules, and gpt-4.1 no better than gpt-4.1-mini, at five times the price. Our
losses were elsewhere: attachments, carrier and trucker answers, pickup places, a new language. gpt-4.1-mini names
German for half of the English mails (German towns, a German company), so the language stays code's reading where
its word lists know the language.

The whole quote written by the model from facts code wrote, checked by code (every number one of the facts' or the
mail's, the total once, carrier, currency, transit), on 14 accepted quotes: gpt-4.1-mini passed 13, gpt-4.1 10
(it left out the transit time three times); about 0.0004 USD and 1.3 seconds each. One passing letter was in German
for an English mail signed "Viele Grüße": a model that picks the language itself can lose a quote as wrong_language.
The letters were no more personal than our template (no goods, no arrival date). Decision: the template stays, and
a language we have no text for is translated from it (`compose.translate`).

gpt-4.1 is not in `llm.PRICES`: the host logs it at `PIXEL_LLM_PRICE_IN`/`_OUT`. Add it before using it.

## 21.11.2026, before the first request

| Model | Mails fully right | Fields right | No answer | Tokens in / out | Seconds | USD per call | Points per call |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-4.1-mini | 5/6 | 41/42 | 0 | 320 / 54 | 0.9 | 0.00021 | 0.09 |
| gpt-4o-mini | 5/6 | 41/42 | 0 | 320 / 54 | 1.3 | 0.00008 | 0.03 |
| gpt-5.4-nano | 3/6 | 39/42 | 0 | 317 / 62 | 0.8 | 0.00014 | 0.06 |
| gpt-5.4-mini | 3/6 | 38/42 | 0 | 317 / 61 | 0.9 | 0.00051 | 0.21 |
| gpt-5.6-luna | 2/6 | 36/42 | 0 | 317 / 87 | 1.1 | 0.00017 | 0.07 |
| gpt-6-luna | 2/6 | 31/42 | 1 | 317 / 140 | 1.8 | 0.00010 | 0.04 |
| gpt-5-mini | 0/6 | 0/42 | 6 | 317 / 200 | 2.6 | 0.00048 | 0.19 |
| gpt-5-nano | 0/6 | 0/42 | 6 | 317 / 200 | 1.2 | 0.00010 | 0.04 |

**Decision: `gpt-4.1-mini`.** It and `gpt-4o-mini` read the mails best and never failed to answer. Between the two
the price gap is 0.06 points per call, and a misread request costs the whole quote; in an earlier run of the same
mails `gpt-4o-mini` converted pounds wrongly once, `gpt-4.1-mini` did not. It also read the scanned tariff of the
start kit into the same nine lanes as a larger model had.

What the numbers say beyond the choice:

- **Reasoning models and a small output limit do not mix.** `gpt-5-nano` and `gpt-5-mini` spent all 200 tokens on
  reasoning and returned nothing; `gpt-6-luna` did so once in six. With 2,000 tokens they answer, slower (5 to 12
  seconds), dearer and no more accurate.
- **Every model is cheap enough.** One call costs between 0.03 and 0.21 points; accuracy decides, not price.
- **The field every model got wrong** is the volume of the mail that gives only dimensions per piece: that is
  arithmetic, and code does it (`agents/rfq/extract.py` `_volume`).
- Six mails are a small sample. Rerun with captured requests once we have them, outside the hours that matter:
  the spend counts in the 18-minute window it happens in.
- The gateway allows 20 calls in flight per team, then answers 429.
- **No audio.** `GET /v1/gateway/models` (21.11.2026, round 67) lists no audio model; `gpt-4o-mini-audio-preview` is
  refused as `model_not_allowed`, and an `input_audio` part to `gpt-6-luna` as "content blocks are expected to be
  either text or image_url". Voice messages are transcribed locally (`agents/tariffs/agent.py` `_transcribe`), never at
  the gateway.
