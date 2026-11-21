# Implementation plan

> **How the system works, what gets built in which order, and who builds what.** Every rule and its number:
> [RULES.md](RULES.md). The challenge: [challenge.md](challenge.md). No value of the start kit appears in this file.

---

## 0. Open questions for the 5U team

Ask these first; each one changes code. The answer goes into [RULES.md](RULES.md) with its source, then the
question is deleted here.

| # | Question | Until answered we assume |
| --- | --- | --- |
| Q1 | Does build-time work count as gateway spend: must parsing the start kit (the scanned tariff) go through the gateway, and does that spend land in "spend per accepted quote in the hour"? | Yes to both: ingestion runs through `llm.py`, once per file, outside the hours we score in where possible |
| Q2 | How is a gateway call tied to a quote for "a quote made without a gateway call earns no cost points": by a time window, by the hour, or by something we send with the call? | By time: one call between the request and our reply |
| Q3 | Is the hourly cost the team's whole gateway spend in that hour divided by its accepted quotes, so spend on rejected quotes, tariff mails and tracking answers counts too? | Yes |
| Q4 | What exactly is the "best price": the cheapest all-in total over every source (sheets, portal, bids, desk, browser page), or the best of the option the customer asked for (the fastest one for "fastest")? Does it include the pickup for door-to-port? | The all-in total of the option the customer asked for, pickup included |
| Q6 | The 50/50 rule among carrier bids: what is the formula (normalised how?), and how does it combine with "cheapest or fastest": does it only pick among mail bids, and do sheets and portal then compete on price alone? | Min-max normalised price and transit, equal weights, among bids only; the winner then competes with the other sources by the customer's wish |
| Q7 | Which currency must the total be in when the cheapest rate is not in EUR? Which exchange rate and rounding does the scorer use? | The rate's own currency and total, compared in EUR with the rate's exchange rate |
| Q8 | Is response time measured from the stored time of the RFQ mail to the stored time of our reply? Is it linear between 2 and 6 minutes? | Yes to both |
| Q9 | Does a reply that is not a quote (a question back, an acknowledgement) use up the "first quote in a thread"? Do RFQs ever lack what we need to price? | Yes, so we never reply before the quote |
| Q10 | How are bookings scored (`booked` on `/v1/board/me`)? What do we book when the quoted price came from a sheet or a mail bid and has no `rate_id`? Must the booking price match the quote? | Book by mail with that airline; no points beyond enabling the tracking bonus |
| Q11 | Tracking: what does `tracking@hackloop.mail` ask, what makes an answer correct (booking reference, status, airline, transit?), and by when must we answer? | It names a shipment; we answer status, airline, lane, booking reference, at once |
| Q12 | What kinds of change come hourly: new tariff versions by mail, portal lanes or limits, mail formats, languages, exchange rates? Is `dataset_version` on a portal rate the signal? | Any of them; we watch `dataset_version`, reason codes and tariff mails |
| Q13 | Pickup: are trucker prices only available by mail? Is the zone the first two digits of the German postcode, and is the airport always the zone's airport? | Yes to all |
| Q14 | Where is the browser rate page, and does it hold rates we cannot get by API, mail or desk? Does asking the rate desk cost anything? | Not needed for the first hours; the desk repeats the portal (R21) |
| Q15 | What is the rate-read limit per minute today, and does it change? | Read it from `X-RateLimit-Limit` on every call |
| Q16 | The +10 human bonus: per accepted mail, or once per team? Who is the reader? | Per mail; every quote is written to be read by a person |
| Q17 | When does scoring end on Sunday, and when is the board frozen (`frozen_at`)? | We run until told |

---

## 1. How we win

1. **Acceptance is a cliff.** A quote more than 5% above the best price scores 0 on all three kinds. Being right
   comes before being cheap or fast: 45 points for accuracy, and the other 55 only exist for accepted quotes.
2. **Cost is nearly free if we are disciplined.** 40 points for staying far below 0.10 USD per accepted quote, but
   zero without any gateway call. One small call per quote keeps almost all of them. The hourly figure is spend per
   *accepted* quote: every wasted call and every rejected quote makes each accepted one more expensive.
3. **Time is the smallest prize and the hardest limit.** 15 points for answering within 2 minutes, nothing at all
   after 6. We aim for 100 seconds and never miss 6 minutes.
4. **The sheets are instant and free.** A request on a lane we hold a tariff for is priced in milliseconds by code.
   The portal costs rate reads; bids cost minutes. The plan is to know before asking who can be cheapest.
5. **Reading closely is the accuracy game** (R13): the right bracket, the minimum, surcharges on the billed weight,
   a cheaper higher break, the right validity. All of it is code with tests against the start kit.
6. **The setup changes hourly.** What survives: tariffs with version and validity, lanes and limits read from the
   API, formats parsed by rules with a model fallback, and a feedback loop from `/v1/board/me` that tells us within
   minutes that quotes started failing.
7. **The jury sees the system.** Sessions UI, dashboard, codebase and the tone of our mails are judged. One event
   log feeds all of it, so what the jury sees is what ran.

## 2. Principles

| # | Principle | Rules |
| --- | --- | --- |
| 1 | Code does the arithmetic and the selection (Decimal, tested). A model only reads what code cannot | P1, R11–R15 |
| 2 | Rules first, model as fallback; one small gateway call per quote by default; tokens and cost logged per call | P2, R28 |
| 3 | Always answer: every request has its own deadline; before it runs out we send the best price we have | P3, R1 |
| 4 | Every mail, decision and model call is an event; CRM and UI read that log | P4, R35 |
| 5 | Nothing from one dataset version is hardcoded; tariffs carry version and validity | P5, R14, R33 |
| 6 | Keys live in `.env` only | P6 |
| 7 | Every change ships with a test or a replayed mail, and its docs in the same commit | R34 |

## 3. Stack

Python 3.12 with uv, FastAPI, SQLite (WAL), httpx, pydantic, pytest. One process (`pixel/app.py`) runs the poller,
the handlers and the UI; the UI is served by the same app and streams with server-sent events. Tariff files are read
with openpyxl, pdfplumber and python-docx. No queue, no worker pool, no ORM, no frontend build: asyncio tasks and
plain SQL are enough for one request every 6 minutes, and one process means one log.

Deviations from this list need a reason written here.

---

## 4. The system

### 4.1 Flow of one request

```
webhook       POST /hook: the mailbox pushes each new mail, signed          mail.in
poller        GET /v1/mailbox/messages?since_id=…  every second, the safety net; a mail is stored once
router        rules on sender, thread, attachments; model only if unsure   route
orchestrator  customer_rfq → session row + a task with a deadline (target 100 s, hard 315 s, expiry 360 s)
  rfq agent     extract (rules, else one model call)                   rfq.extracted
                pricing.engine.options, all sources at once:
                  sheets   store.lookup → calc.price_tariff            option.found   (milliseconds)
                  portal   lanes cached; one rate read per airline     option.found   (seconds)
                  bids     mail to airlines not priced otherwise       bid.requested → bid.received → option.found
                  pickup   door-to-port: mail to the zone's truckers   option.found
                selection.select → compose.render → outbox.send_quote  quote.sent
  fallback      at hard_at without a quote, or at once when the agent fails or returns without one:
                best stored option, code only                          quote.fallback | quote.missed
customer_reply      accept → crm shipment → portal booking by rate_id → customer told    booking.booked
booking_confirmation  "Booking … confirmed" → shipment confirmed → customer told         booking.confirmed
tracking_question   crm.shipments.find → answer in the thread          tracking.answered
tariff              attachments → parse (code, else model) → check → store   tariff.ingested
board feedback      tools/board_me.py every few minutes                score
```

### 4.2 Modules

| Path | What it is |
| --- | --- |
| `pixel/contracts.py` | The data contracts every module imports: `Message`, `Rfq`, `LaneTariff`, `RateOption`, `Quote`, `Booking`, `Shipment`, `Event`, `LlmResult` |
| `pixel/config.py` | Settings from `.env` |
| `pixel/app.py` | Process entry: builds the `Context`, starts the poller, mounts the push endpoint and the UI |
| `pixel/api/` | The 5U API: `client.py` (key header, retries), `mailbox.py`, `portal.py`, `gateway.py`, `board.py` |
| `pixel/core/` | `db.py` (SQLite, schema), `events.py` (the log and its live stream), `poller.py`, `webhook.py` (the mailbox's pushes), `orchestrator.py` (dispatch, deadlines, recovery), `outbox.py` (the only way mail leaves), `context.py` |
| `pixel/agents/router/` | `classify`: which kind of mail this is |
| `pixel/agents/rfq/` | `agent.py` (the quote), `extract.py`, `compose.py` (the mail text), `booking.py` (acceptance, booking, confirmation) |
| `pixel/agents/tracking/` | Answers tracking questions from the CRM |
| `pixel/agents/tariffs/` | Reads tariff files and tariff mails into the store |
| `pixel/pricing/` | `store.py` (tariffs, carriers, zones), `calc.py` (all arithmetic), `portal_rates.py`, `bids.py`, `pickup.py`, `selection.py`, `engine.py` (all sources at once) |
| `pixel/crm/` | `quotes.py`, `bookings.py`, `shipments.py`, `schema.py` |
| `pixel/ui/` | `routes.py` (sessions list, session view, event stream, step in), `dashboard.py` |
| `pixel/llm.py` | The only module that calls the gateway; counts tokens and cost |
| `tools/` | `replay.py`, `board_me.py`, `set_openai_key.py`, `webhook.py` |

### 4.3 Sessions and the event log

A **session** is one customer thread; its id is the id of the thread's first mail. Mail we send to carriers for a
session and their answers belong to the same session: `core/poller.py` `session_of` looks up `in_reply_to`, then
`references` from the parent upwards, and takes the session of the first one we hold (a carrier's answer replies to
our bid request, which `core/outbox.py` stored with the RFQ's session). The rate desk answers in a thread of its
own and names our mail by its subject; `session_of` takes the session of our latest mail to the desk with that
subject. Mail without a customer thread (a tariff mail) is a session of its own. Every outgoing mail is stored with its `purpose` (`bid_request`, `quote`,
`fallback_quote`, `human`, ...); the router reads it to place the answers.

`messages` holds every mail in and out once, by its API id, with the full `Message` as JSON; `sessions` holds one
row per RFQ (customer, state `open`, `quoted` or `missed`, mode `auto` or `human`). Mail leaves only through
`core/outbox.py`, which refuses (`NotSent`) before the API is called: an address outside R5 (`recipient`), dry run
(`dry_run`, emits `mail.held`), a session a person took over (`human`) and a second quote in a session
(`duplicate_quote`). The quote slot is a row `quote:<session>` in `kv`, inserted before the first `await`, so two
racing tasks cannot both send; a failed send frees it for the fallback, a dry-run hold keeps it. The mail limit
(R7) is a one-minute window in the process, and a 429 is waited out, never dropped.

Every step appends an `Event` (`core/events.py` `KINDS`). Tables other than `events` hold current state for fast
queries (`sessions`, `quotes`, `bookings`, `shipments`, `tariffs`); the log is what the UI shows and what
`tools/replay.py` reads. Payloads never contain a key.

### 4.4 Deadlines

`core/orchestrator.py` `deadline_for` gives every request four times, all measured from the stored time of the RFQ
mail, not from when we polled it:

| Time | Default | Meaning |
| --- | --- | --- |
| `target_at` | +100 s | Send by here: full time points with 20 s to spare (R29) |
| `hard_at` | +315 s | The fallback sends the best stored option, built by code alone (P3) |
| `expires_at` | +360 s | The request is lost (R1) |

Before `target_at` the RFQ agent sends as soon as waiting cannot change the answer. Past `target_at` it keeps waiting
only when the request has no price at all yet or when an outstanding bid can still beat the best option; a second
past `target_at` costs about a sixteenth of a point, a quote outside 5% costs everything. These thresholds are
settings, tuned from `/v1/board/me`, never constants in code.

A restart inside a deadline loses nothing: `since_id` is stored after each mail, and `orchestrator.recover`
restarts the task of every open session younger than 6 minutes without a quote and dispatches every stored mail that
was never routed. Tests drive the deadlines with a fake clock (`orchestrator.now`).

### 4.5 Model use

`llm.ask_json` is the only call site. Default per quote: one call. Where rules extract the request with certainty,
the one call is the smallest one that still earns the cost points (R28) and does useful work: checking the
extraction, or the opening line of the mail. Calls happen only for: an RFQ the rules cannot read, a carrier answer
the rules cannot read, a tariff layout the code parsers refuse (the scanned sheet), a customer mail the router
cannot place. Each call has a hard output limit and a per-session budget; a call that would pass the budget raises
and the code path continues without it. Numbers in a mail are formatted by code from the `RateOption`; a model never
writes a price.

### 4.6 Stepping in

A person can take a session over in the UI (`mode = human`): automatic mail for that session is held as drafts and
shown for editing. The deadline fallback still sends unless the person has sent a quote: taking over never causes a
missed request. In `core/outbox.py` mail with purpose `human` (sent by a person from the UI) and the fallback quote
pass a takeover; every other automatic mail of that session is held.

What the sessions UI (`pixel/ui/routes.py`) relies on from the other packages:

- `app.state.ctx` holds the `Context` (set in `pixel/app.py` `lifespan`).
- `route` events carry `{"kind": <MailKind>}`; `option.found` carries `{"option": <RateOption as JSON>}`;
  `mail.held` carries `to`, `subject`, `body` and `reason`, so the person can edit and send the draft.
- The RFQ agent stores its quote with `crm.quotes.save` as a draft and again once sent; the session view and the
  person's send read it from there. While the quote is a draft, what the person sends is that quote
  (`outbox.send_quote`), so first-quote-only and the fallback see it.
- In a session a person took over, the outbox lets `send` with `purpose="human"` through. `send_quote` has no such
  path, so the send route lifts the session to auto only around that one call (`_send_quote_as_person`); the
  outbox's quote claim keeps the agent from sending a second quote meanwhile.
- `mail.held` with reason `human` is shown as a draft; reason `dry_run` is not.

---

## 5. Backlog

P0 is the foundation (§6). Each item names its package; an item is done when its Definition of Done holds.

**P0, a correct quote in time, every 6 minutes** — packages A, B, C below.

**P1, after the first scored hour**

- Read `/v1/board/me` reason codes into the dashboard; alert when two quotes in a row fail (R33).
- Tune `target_at` and the wait-for-bids rule from real response times of carriers.
- Booking by mail when the portal answers 503 or the quote has no `rate_id` (R22).
- Mail tone: language of the customer, short, signed, no template smell (R31).

**P2, robustness against hourly changes**

- New tariff version by mail replaces the old one at its validity date; quotes name the version they used.
- Portal lanes and brackets refreshed on a schedule and after every failed quote.
- Unknown mail kinds land in the UI for a person, not in the bin.
- Replay a failed session against the fixed code before deploying it.

**P3, the final**

- Dashboard polish, session search, the demo path for the pitch.
- Rate desk and browser page as extra sources, if they hold prices the others do not (Q14).

---

## 6. Foundation: three work packages

Three builders, one git worktree each, started from `main` at the skeleton commit. The skeleton fixes the
interfaces: every function a package calls in another package already exists with its signature and docstring.
**A builder edits only the files of its package.** A change to `pixel/contracts.py`, `pyproject.toml` or another
package's signature is a commit of its own on `main`, announced to the other two before it lands.

Until the other packages exist, a builder tests against the signatures with fakes; `PIXEL_DRY_RUN=1` keeps every
run from sending mail or booking.

### Package A: mail pipeline (branch `a/<topic>`)

**Files:** `pixel/app.py`, `pixel/api/client.py`, `pixel/api/mailbox.py`, `pixel/api/board.py`, `pixel/core/*`,
`pixel/agents/router/*`, `tools/replay.py`, `tests/test_api_mailbox.py`, `tests/test_poller.py`,
`tests/test_router.py`, `tests/test_orchestrator.py`, `tests/test_outbox.py`, `tests/fixtures/mails/*`.

**Builds:** the API client with the key header, retries and typed errors; mailbox poll, send, read, attachment;
the poller with a stored `since_id`; the database and the event log with its live stream; the router (rules on
sender, thread and attachments first, one model call through `llm.ask_json` only for a customer mail it cannot
place); the orchestrator with dispatch, a deadline per request, the fallback and recovery after a restart; the
outbox with first-quote-only, recipient check, mail limit, dry run and takeover; the replay tool.

**Definition of Done**

1. `uv run uvicorn pixel.app:app` polls the real mailbox every second, stores every mail exactly once across a
   restart, and each mail shows up as `mail.in` and `route` events with the right session.
2. The first real mails of each kind that arrive are captured as fixtures; `classify` places every fixture
   correctly without a model call; a carrier's answer lands in the session of the RFQ it was requested for.
3. With a fake RFQ agent that never returns, the fallback sends at `hard_at`; with one that has no option,
   `quote.missed` is emitted; with one that returns in time, exactly one quote leaves. Tested with a fake clock.
4. A second `send_quote` for the same session raises `NotSent("duplicate_quote")`, also when two tasks race.
5. `PIXEL_DRY_RUN=1` sends nothing and emits `mail.held`; 429 from the mail limit is waited out, never dropped.
6. `uv run python -m tools.replay <fixture>` runs a stored mail through the whole pipeline without network.
7. No key in any log line, event payload or exception text (test with a marker key).
8. `pixel/agents/router/README.md` and §4 of this plan describe what was built; RULES rows R1–R7 are `done`.

### Package B: pricing (branch `b/<topic>`)

**Files:** `pixel/llm.py`, `pixel/api/portal.py`, `pixel/api/gateway.py`, `pixel/pricing/*`,
`pixel/agents/tariffs/*`, `pixel/agents/rfq/agent.py`, `pixel/agents/rfq/extract.py`,
`pixel/agents/rfq/compose.py`, `tools/set_openai_key.py`, `tests/test_calc.py`, `tests/test_selection.py`,
`tests/test_tariffs.py`, `tests/test_portal.py`, `tests/test_extract.py`, `tests/test_bids.py`,
`tests/test_compose.py`, `tests/test_llm.py`, `tests/fixtures/tariffs/*`.

**Builds:** ingestion of the start kit into the tariff store (code parsers for xlsx, docx, txt and text PDFs; the
scanned sheet through one model call per page; `check` on every result); the portal client with rate-limit
handling; `llm.ask_json` with cost counting; RFQ extraction (rules, then model); bid requests to carriers and
parsing of their answers; pickup; `calc` and `selection`; the quote text.

**Definition of Done**

1. `ingest_folder(data/start-kit)` stores every lane of all ten tariffs with version and validity; a test per file
   type compares parsed lanes with hand-checked ones, and `check` passes on all ten. The scanned sheet is read
   through the gateway once and its result cached in the store, not re-read per quote.
2. `calc.price_tariff` is tested for: volume weight above actual weight, the minimum, surcharges on the billed
   weight, a weight exactly on a break, a cheaper higher break, a top break, rounding. Decimal only.
3. `selection.select` returns the cheapest or the fastest all-in option as asked, compared in EUR;
   `selection.balanced` implements R15; ties are decided by a written rule.
4. The portal client reads the limit from the headers, never spends more reads than the minute allows, waits out a
   429 only when the deadline allows, and turns 404 and 503 into `NotOnPortal` with the hint.
5. `extract` reads every captured RFQ fixture; rules-only for the plain ones, exactly one model call otherwise.
6. For a request no sheet and no portal lane can price, bid requests go to the airlines serving the destination
   and their answers come back as `RateOption`s; door-to-port adds the cheapest pickup of the zone's truckers.
7. `rfq.agent.handle` produces a `Quote` with one all-in total and currency for a replayed RFQ in under 10 s when
   sheets or portal can price it, with at most one gateway call; `fallback` builds one from stored options alone.
8. Every gateway call is an `llm.call` event with model, tokens and cost; `spend_usd` reports spend per session
   and per hour.
9. Agent READMEs, `docs/calc.md` (the arithmetic, written once it exists) and RULES rows R9–R21, R28, R32 are
   current.

### Package C: CRM, booking, tracking, UI (branch `c/<topic>`)

**Files:** `pixel/crm/*`, `pixel/agents/rfq/booking.py`, `pixel/agents/tracking/*`, `pixel/ui/*`,
`tools/board_me.py`, `bench/RUNS.md`, `tests/test_crm.py`, `tests/test_booking.py`, `tests/test_tracking.py`,
`tests/test_ui.py`, `tests/test_board_me.py`.

**Builds:** the CRM tables and queries; the booking flow from the customer's acceptance to the airline's
confirmation; tracking answers; the sessions UI with a live stream and a way to step in; the dashboard; the
feedback loop from `/v1/board/me`.

**Definition of Done**

1. Quotes, bookings and shipments are stored and found again by session, by our reference, by the airline's
   booking reference and by customer and lane.
2. An acceptance mail leads to one shipment with a new id, one portal booking with that id as `team_reference`,
   and a mail to the customer. Tested against a fake portal: 201, 200 on a retry, 410 followed by a fresh rate and
   a second booking, 503 followed by a booking mail to the airline, 409 and 404 as `booking.failed`.
3. "Booking <reference> confirmed" marks the right booking and shipment confirmed and tells the customer once.
4. A tracking question about a booked shipment is answered in its thread from the CRM alone, with no gateway call
   when the reference is in the mail; a question about an unknown shipment gets an honest answer.
5. The sessions list finds a session by customer, lane or reference; the session view shows mails, options with
   prices, decisions and model calls with cost, and updates live over server-sent events without a reload.
6. Take over, edit and send a held draft, release: each works from the session view and is a `human.*` event; the
   deadline fallback still fires for a session that was taken over and not answered.
7. The dashboard shows requests, quoted, missed, accepted and rejected, won and lost, points per kind, spend per
   accepted quote this hour and median response time.
8. `uv run python -m tools.board_me` stores each row of `/v1/board/me` as a `score` event and appends new rows to
   `bench/RUNS.md`; run twice, it adds nothing twice.
9. Agent READMEs and RULES rows R22–R25, R30, R33, R35 are current.

### Merging

Each builder rebases on `main` and merges when its tests pass and `uv run ruff check .` is clean. First merge to
`main` by 15:30 CET on 21.11.2026, whatever state: the three packages must run together once before the first scored
request at 16:00. Order when in doubt: A, then B, then C.

---

## 7. After the foundation: five roles

Once quotes are being accepted, nobody follows packages any more. Five people, one role each; the personas are in
[`README.md`](../README.md#team), the files in [`AGENTS.md`](../AGENTS.md) and
[`.github/CODEOWNERS`](../.github/CODEOWNERS).

| Role | Answers for | Owns | First tasks |
| --- | --- | --- | --- |
| **Cost** | 40 points | `pixel/llm.py`, `pixel/agents/rfq/extract.py`, `pixel/agents/rfq/compose.py`, prompts | Spend per accepted quote per hour; smallest model that extracts reliably; more rules, fewer calls |
| **Speed** | 15 points, and the 6 minute limit | `pixel/core/`, `pixel/agents/router/`, `pixel/api/`, `pixel/app.py` | Response-time histogram; the wait-for-bids rule; no missed request across restarts and API errors; the alert on two failed quotes |
| **Accuracy** | 45 points, and the 5% cliff | `pixel/pricing/`, `pixel/agents/tariffs/`, `tests/` except fixtures | Every rejected quote from `/v1/board/me` becomes a replay fixture and a fix; new tariff versions; Q4–Q7 |
| **Generalization** | Accepted rate after every change | `tests/fixtures/`, Pitfalls sections, the change log in `bench/RUNS.md` | Hand-calculated prices as a second opinion on `calc.py`; parsed tariffs read against their files; written variants of requests and carrier answers; every real mail labelled |
| **Frontend, business and pitch** | UX and design, mail voice, the pitch | `pixel/ui/page.py` | The open questions to 5U (§0); model mails in our voice; the demo path and the pitch |

Shared, reviewed by whoever did not write the change: `pixel/crm/`, `pixel/ui/routes.py`, `pixel/ui/dashboard.py`,
`pixel/agents/rfq/booking.py`, `pixel/agents/tracking/`, `pixel/contracts.py`, `docs/`, `tools/`.

Generalization and Frontend work in files that cannot break a quote; a pipeline owner merges their branches.
Everyone takes a night shift at the dashboard.

Every pull request states what it changes in accepted rate, response time or spend per quote, measured on replayed
fixtures or on `/v1/board/me`, and may not make another KPI worse without saying so.
