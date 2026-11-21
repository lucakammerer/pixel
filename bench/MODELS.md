# Models

Which OpenAI model the gateway calls go to, and why. Measured with `uv run python -m tools.bench_models`: six request
mails written for the tool (German with dimensions and pickup, pounds, a terse one-liner, gross and net weight,
city names, one without the facts), the production extraction prompt and its limit of 200 output tokens, seven
fields per mail. Prices are OpenAI's list prices; "points" is what one call costs of the 40 cost points (R28).

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
  the spend counts in the hour it happens.
- The gateway allows 20 calls in flight per team, then answers 429.
