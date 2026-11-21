# The challenge

START Hack Munich 2026, challenge partner 5U AI: **"Who answers at 2 a.m.?"** Summary of the challenge brief, the
deep-dive slides, the Swagger docs and the start kit as read on 21.11.2026. The rules we build against, numbered:
[RULES.md](RULES.md). The brief and slides are PDFs on the team's machines; they are not in the repo.

## The task

Build a digital air forwarder that quotes, books and tracks shipments through one mailbox, on its own, through the
night. A forwarder buys transport on the customer's behalf: the customer sends an RFQ by mail, we find the best
price among airlines and truckers, quote, book when the customer accepts, and answer "where is it?".

A new air export RFQ reaches every team every 6 minutes and expires 6 minutes later. Every team gets the same
request at the same time and is scored on its own. The first scored request arrives on 21.11.2026 at 16:00 CET;
requests run until Sunday.

One request, three stages:

1. **Quote:** the RFQ arrives, we check rates, ask 2 to 5 carriers, quote within 6 minutes.
2. **Book:** the customer accepts, we book with the rate ID and confirm.
3. **Track:** the customer asks for the status, we answer from our own CRM.

## What we must build

| Part | What the brief asks for |
| --- | --- |
| Router | Reads every mail and routes it: customer request, carrier bid, portal reply, tariff sheet |
| Up to three agents | RFQ and booking; track and trace; tariff ingestion |
| Pricing module | Holds tariffs as structured prices; ingestion of new tariffs is automatic |
| CRM | Quotes and booked shipments: history, won and lost, a dashboard |
| Sessions UI with streaming | Where a person steps into the autonomous system; finding a session and intervening has to be easy |

In scope: air export RFQs only, with a door-to-port option. Out of scope: a TMS, margins, ocean freight,
competition between teams.

## What 5U runs

| Part | What it does |
| --- | --- |
| Mailbox | One per team. Everything arrives there; we poll, and each new mail is also pushed to a webhook we subscribe |
| Rate portal | An API with rate IDs and booking (rate limited, a rate ID is valid 15 minutes), a browser page behind a bot challenge, and a desk at `ratedesk@hackloop.mail` that answers by mail |
| Customer agents | One sends RFQs and follow-ups, one asks tracking questions from `tracking@hackloop.mail` |
| Carrier agents | 2 to 5 per destination answer bid requests by mail. Airlines also send monthly tariffs, truckers do not |
| LLM gateway | One key per team, OpenAI models only, with our own OpenAI key. Counts each team's cost |
| Leaderboard | Each team's score per mail and in total; who booked what stays visible |
| API docs | Swagger at `https://api.start-hack.5u.ai/docs` |

## The start kit (`data/start-kit/`)

Downloaded from `https://api.start-hack.5u.ai/kit/start-kit.zip`. It is one dataset version (tariffs valid
01.11.–30.11.2026); nothing in it may be hardcoded.

- **10 airline tariffs**, each stating currency, validity, a transit time and a minimum per lane, rates per kg by
  weight break, and fuel and security surcharges per kg per lane:

  | File type | Airlines | What differs |
  | --- | --- | --- |
  | xlsx | Elbflug, Kestrel, Vega, Wayzata | blocks per origin or one lane list; decimal comma as text or real numbers; surcharges as columns or as sentences below; transit as `3`, `D+3` or `3 days` |
  | pdf (text) | Cobalt, Helix, Orion | one block per lane or one wide table; `from 0 kg` or `-45`/`+45` headers |
  | docx | Quetzal | a table, surcharges as paragraphs |
  | txt | Yellowtail | fixed-width columns with `EUR` prefixes |
  | jpg, two pages | Danube Wings | a skewed scan: rates on page 1, surcharges on page 2 |

  Shared wording in every sheet: rates are per kg of chargeable weight; chargeable weight is the higher of actual
  weight and volume weight (1 cbm = 167 kg); fuel and security are charged per kg of the weight the freight is
  billed on; the minimum applies to the freight before surcharges.

  What to watch: weight breaks differ per airline (a break at 250 or at 300 kg, a top break at 1000 or at 2000 kg);
  some neighbouring breaks carry the same rate; date formats differ per sheet. The brief warns: "Read the rate
  sheets closely. The best price is not always the obvious one."
- **`carrier-list.csv`** (`name, kind, email, serves`): 29 airlines with the destination airports they serve and
  22 truckers with the pickup zones they serve. Only 10 of the 29 airlines have a sheet in the kit. Four airlines
  serve only destinations no other airline serves. (The second zip on the kit page calls this file `carriers.csv`.)
- **`pickup_zones.csv`** (`zone, area, airport`): 32 two-digit zones, each belonging to one of six German origin
  airports. The kit holds no trucker prices.

## Scoring

Each accepted quote earns up to 100 points; a team's score is the sum over all requests.

| Kind | Points | How |
| --- | --- | --- |
| Price accuracy | 45 | Full at the best price, fewer the further above it |
| LLM cost | 40 | Gateway spend per accepted quote in the hour: full at no spend, zero at 0.10 USD. A quote made without a gateway call earns none. Added once an hour |
| Response time | 15 | Full within 2 minutes of the request, zero at 6 |
| Tracking bonus | +10 | A correct tracking answer on a booked shipment |
| Human bonus | +10 | From the jury, for the accepted mail that reads most human |

A quote is accepted when it is within 5% of the best price, is our first reply in the thread and arrives within
6 minutes. Only accepted quotes score. Among carrier bids the right one is the most balanced: price and speed weigh
50/50. The weights above are those of the brief and of `GET /v1/board` (`rules`); the slides show 55/30/15 and are
taken as outdated.

After a few hours of build time the setup changes about once an hour, unannounced. Teams learn of a change from
failed quotes.

## The final

The leaderboard alone does not pick the winner. Every team pitches: 3 minutes plus 2 minutes of questions. The jury
judges the pitch, UX and design, the codebase (tech depth) and how human the mails read.
