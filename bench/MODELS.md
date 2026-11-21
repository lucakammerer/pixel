# Models

Which OpenAI model the gateway calls go to, and why. Measured with `uv run python -m tools.bench_models`: six request
mails written for the tool (German with dimensions and pickup, pounds, a terse one-liner, gross and net weight,
city names, one without the facts), the production extraction prompt and its limit of 200 output tokens, seven
fields per mail. Prices are OpenAI's list prices; "points" is what one call costs of the 40 cost points (R28).

## 21.11.2026, round 74: a second reader for every request

Do the top teams buy accuracy with their spend (START Milan about 0.0085 USD per accepted quote, we 0.0035)? Measured
on the live host, dry and on a copy of the database.

**On requests the rules already read**, the 44 accepted ones without attachments: rules, gpt-4.1-mini and gpt-4.1
each 42/44, the same two misses. That alone says nothing: the rules were patched on exactly these mails.

**At first sight**, each lost round read again by the production prompt with today's code arithmetic (`_from_model`):

| Lost round | What went wrong then | gpt-4.1-mini | gpt-4.1 |
| --- | --- | --- | --- |
| 9 | the model added three lots itself | right | right |
| 33, 34, 35 | the mail's volume or weight over the packing list's | wrong (the mail's) | right |
| 70, 72 | French, Chinese answered in English | right (fr, zh) | right (fr, zh) |
| 15 to 18 | a pickup "from our warehouse" with no place | no reading | no reading |

gpt-4.1-mini names German for half of the English mails with German towns; gpt-4.1 for one of 44.

**Carrier and trucker answers**, 359 of rounds 9 to 72 with their attachments' text: gpt-4.1-mini and gpt-4.1 read
the same total as today's rules in every one. In round 67 the rules then read a waiting-time line (45.00 EUR) as the
offer; the model reads 181.96.

**Decision:** gpt-4.1 reads every request the rules read too (`extract._crosscheck`, `PIXEL_MODEL_CHECK`); a value it
reads for weight, volume, pieces, preference or a destination the text names wins. It also reads a request the rules
cannot, in place of gpt-4.1-mini. On 12 accepted and 7 lost requests end to end it changed nothing the scorer had
accepted, and set rounds 33 to 35 right. About 0.0027 USD and 1.5 seconds per request, in place of the prose call:
about 1.1 cost points per quote, against about 100 points for each rejection it prevents. gpt-4.1-mini stays for
the rest, and reads every answer the rules price too (`bids._crosscheck`): its total wins where they differ, round
67's case, at about 0.0002 USD per answer, 0.6 cost points per quote.

The whole quote written by the model, checked by code, on 14 accepted quotes: gpt-4.1-mini passed 13, gpt-4.1 10
(it left out the transit time three times). One passing letter was in German for an English mail signed "Viele
Grüße", and the letters were no more personal than our template. The template stays; a language without our text is
translated from it (`compose.translate`).

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
