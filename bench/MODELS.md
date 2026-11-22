# Models

Which OpenAI model the gateway calls go to, and why. Measured with `uv run python -m tools.bench_models`: six request
mails written for the tool (German with dimensions and pickup, pounds, a terse one-liner, gross and net weight,
city names, one without the facts), the production extraction prompt and its limit of 200 output tokens, seven
fields per mail. Prices are OpenAI's list prices; "points" is what one call costs of the 40 cost points (R28).

## 22.11.2026: model calls only where the check or a second reader doubts

Not measured on the live key yet; offline the replayed fixtures make the same calls as before.

- `compose.prose` (the call on every request the rules read) also reads destination, weight and volume: about 30
  output tokens more on `PIXEL_MODEL_PROSE`, no further call.
- Where it disagrees with the rules, gpt-4.1 reads the request (`extract._crosscheck` via `agent._second_read`):
  about 0.0027 USD, about 1 cost point, on the requests with a dissent only.
- `PIXEL_SECOND_READ=log` records every dissent without the gpt-4.1 call; `off` sends the name-only prompt again.
- Measure after a live hour: share of requests with a dissent (`rfq.second_read` with `dissent`) and how often it
  changed the quoted price.

## 21.11.2026, round 83: what measuring on the live key cost

The scorer's cost points per 18-minute window against the spend the live service logged (`llm.call` events). Where
no one else spent, they match: rounds 58 to 60 scored 39.86, 0.00035 USD per quote, the service logged 0.00104 USD
for the three. From 21:00 UTC the measuring below ran on the live host with the live key, in scored windows:

| Rounds (accepted) | Cost points | Spend the scorer counted | The service logged |
| --- | --- | --- | --- |
| 61, 62 | 34.19 | 0.029 USD | 0.002 USD |
| 71 | 29.48 | 0.026 USD | 0.001 USD |
| 73, 74 | 7.30 | 0.164 USD | 0.002 USD |
| 75 | 0.00 | at least 0.100 USD | 0.001 USD |
| 80 | 20.65 | 0.048 USD | 0.001 USD |

About 145 cost points lost on six accepted quotes. After it, the second readers went live: gpt-4.1 on every request
and gpt-4.1-mini on every priced carrier answer, 0.0079 USD for rounds 81 to 83 against about 0.0011 before, about
1.6 cost points per quote in place of 0.14. Rounds 33 to 35, the case for gpt-4.1, all had a packing list
attached, and the rules refuse round 67's waiting rate themselves, so gpt-4.1 now reads only a request with
attachments (27 % of requests since 17:00 UTC) and a carrier answer costs a call only when the rules cannot read
it: about 0.4 cost points per quote. Round 67's offer stood in a trucker's attachment, a layout the rules had not
seen, so gpt-4.1-mini still reads an offer that stands only in an attachment (about 0.0002 USD, 0.07 cost points
per answer); an answer in the mail itself costs no call. Measure offline, or right after a quote went out and on
few mails.

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

**Decision:** gpt-4.1 reads every request the rules read too (`extract._crosscheck`, `PIXEL_MODEL_CHECK`). Pieces and
the wish win as it reads them; weight and volume only when code reads the figure as a gross total with its unit (after
round 82, "95.00 cubic feet" taken as 95 cbm, and the review: a net weight, an airport named in passing). It also reads a request the rules
cannot, in place of gpt-4.1-mini. On 12 accepted and 7 lost requests end to end it changed nothing the scorer had
accepted, and set rounds 33 to 35 right. About 0.0027 USD and 1.5 seconds per request, in place of the prose call:
about 1.1 cost points per quote, against about 100 points for each rejection it prevents. gpt-4.1-mini stays for
the rest.

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
