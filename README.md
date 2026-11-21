# PIXEL

A digital air forwarder for the 5U AI challenge at START Hack Munich 2026, "Who answers at 2 a.m.?". It reads one
mailbox, prices every air export request from tariff sheets, the rate portal and carrier bids, quotes within
minutes, books when the customer accepts and answers tracking questions from its own CRM. A person can watch every
session live and step in.

```mermaid
flowchart TD
    MB[(5U mailbox)] -->|poll every second| P[poller]
    P --> R{router}
    R -->|customer request| O[orchestrator<br/>deadline per request]
    R -->|carrier bid, desk answer| BID[pricing: bids]
    R -->|tariff mail| T[tariff agent]
    R -->|acceptance, booking confirmation| BK[booking]
    R -->|tracking question| TR[tracking agent]

    O --> RFQ[RFQ agent<br/>extract: rules, else one model call]
    RFQ --> ENG[pricing engine]
    T --> ST[(tariff store<br/>version + validity)]
    ST --> ENG
    PORTAL[rate portal] --> ENG
    BID --> ENG
    ENG --> SEL[calc + selection<br/>code, Decimal]
    SEL --> Q[quote text]
    Q --> OUT[outbox<br/>first quote only]
    O -.->|hard deadline: best price so far| OUT
    OUT --> MB

    BK -->|book by rate id| PORTAL
    BK --> CRM[(CRM<br/>quotes, bookings, shipments)]
    TR --> CRM
    TR --> OUT
    BK --> OUT

    RFQ -.-> LLM[llm.py] -.-> GW[5U gateway]
    P & R & O & RFQ & ENG & OUT & BK & TR & T & LLM --> EV[(event log)]
    EV --> UI[sessions UI, live<br/>step in]
    EV --> DASH[dashboard]
    BOARD["GET /v1/board/me"] --> EV
```

## Run it

```sh
uv sync
cp .env.example .env                       # fill in the keys; PIXEL_DRY_RUN=1 sends nothing
uv run python -m tools.set_openai_key      # once
uv run uvicorn pixel.app:app --port 8000   # http://localhost:8000
uv run pytest -q
```

## How it scores

Up to 100 points per accepted quote: 45 for price accuracy, 40 for low model cost, 15 for response time. A quote is
accepted only within 5% of the best price, and only as our first reply within 6 minutes. So: code does all the
arithmetic, a model is called once per quote at most, and every request has a deadline that always ends in an
answer.

## Read on

- [`AGENTS.md`](AGENTS.md): for coding agents: non-negotiables, where things live, who owns what, commands
- [`docs/challenge.md`](docs/challenge.md): the challenge as given
- [`docs/RULES.md`](docs/RULES.md): every rule we are scored by, numbered, with its source and its place in code
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md): open questions, the system, the backlog, the work split
- [`bench/RUNS.md`](bench/RUNS.md): our results per request

## Status

The repo holds the plan and the typed skeleton. Every function is a stub with its contract; the three foundation
packages fill them in ([plan §6](docs/IMPLEMENTATION_PLAN.md#6-foundation-three-work-packages)).
