# Implementation plan

> **How the system works, what gets built in which order, and who builds what.** Every rule and its number:
> [RULES.md](RULES.md). The challenge: [challenge.md](challenge.md). No value of the start kit appears in this file.

---

## 0. Open questions for the 5U team

Ask these first; each one changes code. The answer goes into [RULES.md](RULES.md) with its source, then the
question is deleted here.

| # | Question | Until answered we assume |
| --- | --- | --- |
| Q1 | Does build-time work count as gateway spend: must parsing the start kit (the scanned tariff) go through the gateway, and does that spend land in the window's spend per accepted quote? | Yes to both: ingestion runs through `llm.py`, once per file, outside the hours we score in where possible |
| Q2 | How is a gateway call tied to a quote? | Answered on 21.11.: not at all. The spend of an 18-minute window is shared over the quotes accepted in it; only a window without any spend earns no cost points (R28) |
| Q3 | Is the cost the team's whole gateway spend in that window divided by its accepted quotes, so spend on rejected quotes, tariff mails and tracking answers counts too? | Yes |
| Q4 | What exactly is the "best price": the cheapest all-in total over every source (sheets, portal, bids, desk, browser page), or the best of the option the customer asked for (the fastest one for "fastest")? Does it include the pickup for door-to-port? | The all-in total of the option the customer asked for, pickup included |
| Q6 | The 50/50 rule among carrier bids: what is the formula (normalised how?), and how does it combine with "cheapest or fastest": does it only pick among mail bids, and do sheets and portal then compete on price alone? | Among trucker bids only, price and speed as multiples of the best (`docs/calc.md`); airline bids compete one by one with the other sources by the customer's wish (round 28) |
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
   zero for an 18-minute window without any gateway call. One small call per quote keeps almost all of them. The
   figure is the window's spend per *accepted* quote: every wasted call and every rejected quote makes each
   accepted one more expensive.
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
poller        GET /v1/mailbox/messages?since_id=…  at the allowed cadence, the safety net; a mail is stored once
router        rules on sender, thread, attachments; model only if unsure   route
orchestrator  customer_rfq → session row + a task with a deadline (target 100 s, hard 315 s, expiry 360 s)
  rfq agent     read customer mail and attachment facts                 rfq.extracted
                novelty: sentences no rule read → one model call, alert  rfq.novelty
                pricing.engine.options:
                  sheets   publish local prices                         option.found
                  portal   actual weight, then the next break           option.found
                  bids     mail carriers with outstanding prices        bid.requested → bid.received
                           portal outage + sheet price: pending desk until 115 s   bid.desk_wait
                  pickup   mail truckers for a door-to-port request     option.found
                selection.select → compose.render → outbox.send_quote  quote.sent
  recovery      unanswered after 150 s or handler exits                quote.recovery
  fallback      watch stored options for a complete, compliant price              quote.waiting
                hard deadline: send available scope or explain the miss  quote.fallback | quote.missed, mail.sent
customer_reply      followup.read: phase, intent, changes                                  followup.read
                    change before the acceptance → requote in reply to the change mail   quote.sent
                    affirmative booking of a delivered quote → shipment → book by rate id or mail
                    tracking after the acceptance → tracking agent; thank-you → nothing
booking_confirmation  "Booking … confirmed" → shipment confirmed → customer told         booking.confirmed
                      pending or partial → retain status and reference; alert the operator
tracking_question   CRM lookup → one identified shipment or a request for its reference   tracking.answered
tariff              attachments → parse → validate → activate          tariff.ingested | tariff.failed
                    dated carrier notice → new booking availability    carrier.availability
feedback            core/feedback.py every 10 s: points per request, the option the scorer's best price was,
                    the balanced rule that fits; what needs repair goes to PIXEL_ALERT_URL        feedback
watchdog            core/watchdog.py every 5 s: a request without our quote 90 s after it was stored
                    goes once to PIXEL_ALERT_URL with its age and the step it hangs in   quote.stuck
```

### 4.2 Modules

| Path | What it is |
| --- | --- |
| `pixel/contracts.py` | The data contracts every module imports: `Message`, `Rfq`, `LaneTariff`, `RateOption`, `Quote`, `Booking`, `Shipment`, `Event`, `LlmResult` |
| `pixel/config.py` | Settings from `.env` |
| `pixel/app.py` | Process entry: builds the `Context`, starts the poller, mounts the push endpoint and the UI |
| `pixel/api/` | The 5U API: `client.py` (key header, retries), `mailbox.py`, `portal.py`, `gateway.py`, `board.py` |
| `pixel/core/` | `db.py` (SQLite, schema), `events.py` (the log and its live stream), `poller.py`, `webhook.py` (the mailbox's pushes), `orchestrator.py` (dispatch, deadlines, recovery), `outbox.py` (the only way mail leaves; every quote with itself as a PDF, `pdf.py`), `context.py` |
| `pixel/agents/router/` | `classify`: which kind of mail this is |
| `pixel/agents/rfq/` | `agent.py` (the quote), `extract.py`, `compose.py` (the mail text), `booking.py` (acceptance, booking, confirmation) |
| `pixel/agents/tracking/` | Answers tracking questions from the CRM |
| `pixel/agents/tariffs/` | Reads tariff files and tariff mails into the store |
| `pixel/pricing/` | `store.py` (tariffs, carriers, zones), `calc.py` (all arithmetic), `portal_rates.py`, `bids.py`, `pickup.py`, `selection.py`, `engine.py` (all sources at once) |
| `pixel/crm/` | `quotes.py`, `bookings.py`, `shipments.py`, `customers.py`, `carriers.py` (scorecard per carrier from the event log), `schema.py` |
| `pixel/ui/` | `routes.py` (sessions list, session view, event stream, step in), `dashboard.py` |
| `pixel/llm.py` | The only module that calls the gateway; counts tokens and cost |
| `tools/` | `replay.py`, `kpi.py` and `kpi_pr.sh` (accuracy, cost and speed on replayed requests, compared with the base in every pull request), `board_me.py`, `set_openai_key.py`, `webhook.py` |

### 4.3 Sessions and the event log

A **session** is one customer thread; its id is the id of the thread's first mail. Mail we send to carriers for a
session and their answers belong to the same session: `core/poller.py` `session_of` looks up `in_reply_to`, then
`references` from the parent upwards, and takes the session of the first one we hold (a carrier's answer replies to
our bid request, which `core/outbox.py` stored with the RFQ's session). The rate desk answers in a thread of its
own and names our mail by its subject; `session_of` takes the session of our latest mail to the desk with that
subject. Mail without a customer thread (a tariff mail) is a session of its own. Every outgoing mail is stored with its `purpose` (`bid_request`, `quote`,
`fallback_quote`, `human`, ...); the router reads it to place the answers.

The poller stores the incoming mail and a pending `mail_work` row together. The router records the kind, and
`orchestrator.dispatch` marks the work complete after the handler returns. A restart can therefore resume a
classified tracking question, bid or booking reply whose handler was interrupted.

`core/outbox.py` waits for a mail slot before it durably claims delivery. A confirmed mailbox receipt records the
outgoing message, event and the sent quote together. A definite rejection releases the claim. If the process loses
the response after submitting a mail, the claim stays pending to prevent a duplicate. A later reply that names the
missing parent can confirm delivery through `outbox.reconcile_reply`. Without a matching receipt, delivery remains
unconfirmed. A quote is the exception: any failed send releases its claim, so the fallback still answers (R1); only
the first quote counts (R4), so a second one after a lost receipt is harmless.

Every step appends an `Event` (`core/events.py` `KINDS`). Tables other than `events` hold current state for fast
queries (`sessions`, `quotes`, `bookings`, `shipments`, `tariffs`); the log is what the UI shows and what
`tools/replay.py` reads. Payloads never contain a key.

### 4.4 Deadlines

`core/orchestrator.py` `deadline_for` and `Deadline.recovery_at` define the request's deadlines, measured from the stored time of the RFQ
mail, not from when we polled it:

| Time | Default | Meaning |
| --- | --- | --- |
| `target_at` | +100 s | Send by here: full time points with 20 s to spare (R29) |
| `recovery_at` | +150 s | Hand an unanswered request to recovery |
| `hard_at` | +315 s | The fallback sends the best stored option, built by code alone (P3) |
| `expires_at` | +360 s | The request is lost (R1) |

Sheets are priced locally. The portal publishes actual-weight prices before optional higher-break reads. Carrier
mail can start while portal reads are pending. Bid collection ends when all expected replies arrive or its window
closes. That window runs until the later of `target_at` and `PIXEL_BID_WAIT_S` after the enquiries, capped at
`hard_at`. A late start therefore still leaves time for a trucker to price the pickup.

If the live agent finishes without sending or reaches `recovery_at` (`hard_at` for a request first seen after
`recovery_at`: recovery asks no airline), the orchestrator takes over and checks stored
options for the current lane until a complete price meets explicit airline, arrival and transit requirements, or
`hard_at` is reached. At `hard_at`, a quote can give an alternative and state the requirement it could not meet. It gives an in-flight quote time
to finish. In the meantime, `core/quote_recovery.py` `source` has up to 20 seconds before `hard_at` to recheck sheets
and the portal for missing air prices and request pickup bids from eligible truckers without an enquiry for the
pickup. It uses the normal extraction path if no RFQ was stored and the rules can read the mail; otherwise the
handler's model read, finished or cancelled, was the request's one gateway call.
A door request needs a priced trucker before `hard_at`. At
`hard_at`, an available airport-to-airport price may be sent with pickup excluded and `complete_service=false`. With no freight rate,
`_no_price_reply` sends one reply through the outbox and the request remains recorded as a scoring miss.
A missing postcode prompts a request for the pickup address in that quote.

Offline KPI replay advances its clock through bid waits and deadlines. A replay with scheduled replies can still
receive a captured late bid. A single-request online replay can skip the final wait for incoming mail.

`orchestrator.recover` repairs receipts, resumes unfinished handlers and applies the original expiry to RFQs.
`tools.window` measures the deployment window from the latest RFQ's stored receipt time when available.

### 4.5 Model use

All operational model calls pass through `llm.ask_json`. A request read by rules usually gets one call for
short prose slots. If the model reads the request, the same call supplies those slots. Other calls cover an
uncertain route, a guessed airport outside the served set, a carrier reply the rules cannot parse, or transcription
of a tariff image or PDF page. Calls have output limits. Session calls check the recorded session spend before
starting. Code calculates and formats customer prices. Gateway failures are reported using a safe error category.

### 4.6 Stepping in

During takeover, airline, trucker and rate-desk sourcing can continue. `ui/operator.py` `source` lets the operator
record shipment facts after a stopped extraction, look up sheet and portal rates, and send tracked bid requests.
`ui/operator.py` `source` uses the RFQ agent's quote builder and existing selection rules to prepare a draft from
saved rates. The operator can edit the text and send through `outbox.send_quote(operator=True)`. The send route checks
the draft identity, total, currency, carrier and transit. The outbox blocks a free-text first reply to the customer.
Other held customer mail appears in the session as drafts. See [the operator walkthrough](MANUAL_TAKEOVER.md).

If the operator leaves the request unanswered, the automatic fallback may send at `hard_at`. Release restores
automatic handling before then. Both sends use the same session quote claim and the CRM stores the delivered offer.

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
3. With a fake RFQ agent that never returns, recovery takes over at `recovery_at`; with one that has no option,
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
| **Cost** | 40 points | `pixel/llm.py`, `pixel/agents/rfq/extract.py`, `pixel/agents/rfq/compose.py`, prompts | Spend per accepted quote per 18-minute window; smallest model that extracts reliably; more rules, fewer calls |
| **Speed** | 15 points, and the 6 minute limit | `pixel/core/`, `pixel/agents/router/`, `pixel/api/`, `pixel/app.py` | Response-time histogram; the wait-for-bids rule; no missed request across restarts and API errors; the alert on two failed quotes |
| **Accuracy** | 45 points, and the 5% cliff | `pixel/pricing/`, `pixel/agents/tariffs/`, `tests/` except fixtures | Every rejected quote from `/v1/board/me` becomes a replay fixture and a fix; new tariff versions; Q4–Q7 |
| **Generalization** | Accepted rate after every change | `tests/fixtures/`, Pitfalls sections, the change log in `bench/RUNS.md` | Hand-calculated prices as a second opinion on `calc.py`; parsed tariffs read against their files; written variants of requests and carrier answers; every real mail labelled |
| **Frontend, business and pitch** | UX and design, mail voice, the pitch | `pixel/ui/page.py` | The open questions to 5U (§0); model mails in our voice; the demo path and the pitch |

Shared, reviewed by whoever did not write the change: `pixel/crm/`, `pixel/ui/routes.py`, `pixel/ui/dashboard.py`,
`pixel/agents/rfq/booking.py`, `pixel/agents/tracking/`, `pixel/contracts.py`, `docs/`, `tools/`.

Generalization and Frontend work in files that cannot break a quote; a pipeline owner merges their branches.

**Channel matrix.** 5U raises the difficulty one channel at a time: rounds 63 to 67 were lost because carriers
suddenly answered only in attachments, a channel no fixture or generated case had, while our cases went deep on
shapes already seen. `tools/benchmark_generalization.py` section 12 holds every mail kind the router knows against
every place its information can sit (body; pdf, scan, docx, xlsx, txt or csv, audio attachment; body and attachment
disagreeing); each cell is covered by a case or listed with its owner in `tests/fixtures/channel-gaps.json`, and
`--gaps` prints the holes. The rule: every new shape seen live adds its cell's neighbours, the same kind in every
other carrier of information, before the next one goes live.
Everyone takes a night shift at the dashboard.

Every pull request states what it changes in accepted rate, response time or spend per quote, measured on replayed
fixtures or on `/v1/board/me`, and may not make another KPI worse without saying so. The replayed part is posted
by CI: `tools/kpi.py` `report`, run by `.github/workflows/ci.yml` on the base and on the branch, offline on the
mail fixtures. `tools/kpi_pr.sh` runs it on the live host instead: the stored requests with the carriers' answers,
the real portal and gateway, cost in USD and time to the quote in seconds, each with its points (R28, R29).
