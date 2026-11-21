# AGENTS.md

We are team PIXEL at START Hack Munich 2026, challenge 5U AI: "Who answers at 2 a.m.?". We build a digital air
forwarder that quotes, books and tracks shipments through one mailbox, on its own, through the night. A new request
arrives every 6 minutes and expires 6 minutes later. This file is for you as a coding agent; humans read
`README.md`.

## Non-negotiables

- **Always answer.** A missed request is lost for good (R1). Every request has its own deadline; before it runs out
  the fallback sends the best price we have. Nothing you add may block that path.
- **One quote per thread.** Only our first quote in a customer's thread counts (R4). Mail leaves through
  `pixel/core/outbox.py` and nowhere else; never reply to a customer before the quote, except one question for a
  fact the price needs and the request left out (`extract.py` `_ask`).
- **Code does the arithmetic and the selection.** Every calculation lives in `pixel/pricing/calc.py`, every choice
  between options in `pixel/pricing/selection.py`: Decimal, tested. A model never computes, compares or writes a
  price.
- **Rules first, model as fallback.** A model only reads what code cannot: an unusual request, an odd carrier
  answer, an unknown tariff layout. One small gateway call per quote by default (R28: none earns no cost points,
  0.10 USD per accepted quote earns none either).
- **All model calls go through `pixel/llm.py`,** which calls the 5U gateway and logs tokens and cost. No other
  module imports an LLM client or `pixel/api/gateway.py`.
- **Everything is an event.** Every mail in and out, every decision and every model call is written with
  `core.events.emit`. The CRM and the UI read that log; nothing keeps state only in memory.
- **Nothing from one dataset version is hardcoded.** No lane, rate, airline, address, limit or zone in code, prompts
  or tests outside `tests/fixtures/` and `data/start-kit/`. Tariffs carry a version and a validity. The setup
  changes hourly.
- **The repo is public.** Keys live in `.env` only. Never print, log, commit or put a key into an event, a test or
  an error message.
- **No margin.** We quote the price we buy at (R17).

## Where things live

- `pixel/contracts.py`: the data contracts all modules share (`Message`, `Rfq`, `LaneTariff`, `RateOption`,
  `Quote`, `Booking`, `Shipment`, `Event`). Changed only on `main`, in a commit of its own.
- `pixel/app.py`: process entry; one process runs poller, push endpoint, handlers and UI
- `pixel/api/`: the 5U API (`client.py`, `mailbox.py`, `portal.py`, `gateway.py`, `board.py`)
- `pixel/core/`: `poller.py`, `webhook.py` (the mailbox's pushes), `orchestrator.py` (dispatch, deadlines, fallback),
  `outbox.py` (all outgoing mail), `events.py` (the log and its live stream), `db.py`, `context.py`;
  `feedback.py` (what each request scored, why it lost points, which balanced rule fits the scorer, and the
  call to `PIXEL_ALERT_URL` when something needs repair)
- `pixel/agents/<name>/`: one folder per agent with `agent.py` and `README.md`: `router`, `rfq` (plus
  `extract.py`, `compose.py`, `booking.py`), `tracking`, `tariffs`
- `pixel/pricing/`: `store.py` (tariffs, carriers, zones), `calc.py`, `selection.py`, `portal_rates.py`,
  `bids.py`, `pickup.py`, `engine.py`
- `pixel/crm/`: quotes, bookings, shipments, customers; `pixel/ui/`: requests (`routes.py`), CRM viewer (`crm.py`),
  performance (`dashboard.py`), shared HTML and styles (`page.py`)
- `pixel/llm.py`: the only gateway caller
- `docs/`: `RULES.md` (every rule we are scored by, numbered), `IMPLEMENTATION_PLAN.md` (system, backlog, work
  split, open questions), `challenge.md`; start at `docs/README.md`
- `tests/`: unit tests and `fixtures/` (captured mails, tariff expectations); `tools/`: `replay.py`, `kpi.py`, `simulate.py`,
  `kpi_pr.sh`, `board_me.py`, `set_openai_key.py`, `webhook.py`, `preview.py`, `deploy.sh`, `window.py`, `cd_status.py`, `benchmark_generalization.py`, `document_conflicts.py` (its section 11: mail against attached document); `deploy/`: the service unit and the host's deploy job (`cd.sh`, `pixel-cd.{service,timer}`); `bench/RUNS.md`: our results per request; `bench/REVIEWS.md`: each reviewed pull request by head commit;
  `data/start-kit/`: the kit as downloaded

## Who owns what

**Foundation** ([plan §6](docs/IMPLEMENTATION_PLAN.md#6-foundation-three-work-packages)): three packages, one
worktree each, no shared files.

- **A, mail pipeline:** `pixel/app.py`, `pixel/api/{client,mailbox,board}.py`, `pixel/core/`,
  `pixel/agents/router/`, `tools/replay.py`
- **B, pricing:** `pixel/llm.py`, `pixel/api/{portal,gateway}.py`, `pixel/pricing/`, `pixel/agents/tariffs/`,
  `pixel/agents/rfq/{agent,extract,compose}.py`, `tools/set_openai_key.py`
- **C, CRM, booking, tracking, UI:** `pixel/crm/`, `pixel/ui/`, `pixel/agents/rfq/booking.py`,
  `pixel/agents/tracking/`, `tools/board_me.py`, `bench/`

**After the foundation** ([plan §7](docs/IMPLEMENTATION_PLAN.md#7-after-the-foundation-five-roles)): five
roles, one number each; the personas are in [`README.md`](README.md#team). File owners in
[`.github/CODEOWNERS`](.github/CODEOWNERS) review every change to their files.

- **Cost** (40 points): `pixel/llm.py`, `pixel/agents/rfq/extract.py`, `pixel/agents/rfq/compose.py`, prompts
- **Speed** (15 points and the 6 minute limit): `pixel/core/`, `pixel/agents/router/`, `pixel/api/`, `pixel/app.py`
- **Accuracy** (45 points and the 5% cliff): `pixel/pricing/`, `pixel/agents/tariffs/`, `tests/` except fixtures
- **Generalization** (accepted rate after a change): `tests/fixtures/`, the Pitfalls sections of the agent READMEs
- **Frontend, business and pitch** (UX, mail voice, the pitch): `pixel/ui/page.py`
- **Shared:** `pixel/crm/`, `pixel/ui/routes.py`, `pixel/ui/dashboard.py`, `pixel/agents/rfq/booking.py`,
  `pixel/agents/tracking/`, `pixel/contracts.py`, `docs/`, `tools/`

Edit only your files. If you need another owner's function to behave differently, change its signature on `main` in
a commit of its own and tell the others first. Every pull request states its effect on accepted rate, response time
or spend per quote.

## Commands

```sh
uv sync                                         # Python 3.12 and dependencies
cp .env.example .env                            # then fill in the keys; PIXEL_DRY_RUN=1 sends nothing
uv run python -m tools.set_openai_key           # once: store our OpenAI key at the gateway
uv run uvicorn pixel.app:app --port 8000        # poller + handlers + UI on http://localhost:8000
uv run pytest -q                                # seconds, no network
uv run ruff check . && uv run ruff format .
uv run python -m tools.replay tests/fixtures/mails/<file>.json   # one stored mail through the pipeline
uv run python -m tools.replay --db <database> --session <id>     # a stored session on a copy of that database
uv run python -m tools.kpi                      # accuracy, cost and speed on the replayed request fixtures, as JSON
uv run python -m tools.benchmark_generalization # fixtures vs code: prices, sheets, wording, routing, bids; exit 1 on a fail
uv run python -m tools.benchmark_generalization --gaps   # open known gaps, grouped by owner
uv run python -m tools.simulate                 # generated mails and attachments per axis: right, model, wrong
tools/kpi_pr.sh <pull request>                  # the same online on the live host, posted as a comment
uv run python -m tools.board_me                 # our results from /v1/board/me into bench/RUNS.md
uv run python -m tools.attention --minutes 30   # what went wrong lately: where a repair starts
uv run python -m tools.bench_models             # candidate models on request mails: accuracy, tokens, cost (spends!)
uv run python -m tools.ingest data/start-kit    # tariffs, carrier list and pickup zones into the store
uv run python -m tools.preview                  # the UI on synthetic data in data/preview.sqlite3; sends nothing
uv run python -m tools.webhook <public url>/hook  # push new mail to that endpoint, then test it; no url: last pushes
tools/deploy.sh --after-quote                   # by hand, only when the host's deploy job is paused: this checkout to the live host in the next safe window
ssh luca-kammerer 'sudo -u pixel python3 /opt/pixel/app/tools/cd_status.py'   # the host's deploy job: live, main, last decision, paused
```

Only one process may run with `PIXEL_DRY_RUN=0` against the real mailbox: the `pixel` service on the live host
(`deploy/pixel.service`; its `.env` and database live only there). A unit drop-in on the host binds the UI to the
address its reverse proxy reaches, and the proxy asks for a login. The mailbox pushes new mail to `POST /hook`, which
must pass the proxy without that login: a push is checked by its signature (`PIXEL_WEBHOOK_SECRET`). One endpoint
per team: subscribing another URL takes the pushes away from the live host, which then only polls. A restart waits at most 3 seconds for open
connections: the UI's live stream would otherwise hold the service in shutdown past a request's deadline.
Everything else runs dry or on replayed mail.

## Branches

Branch from `main`: `accuracy/<topic>`, `speed/<topic>`, `cost/<topic>`. Small pull requests, one at a time per
thread, the subject says what changed, no trailers. How a change reaches `main` and the live host:

1. **The gate is local.** Update the branch once with `origin/main`, then run `uv run pytest -q`,
   `uv run ruff check .` and `uv run python -m tools.kpi` (the mail fixtures replayed offline: sheet prices only, no
   model). Green means: push, open the pull request, merge it at once (`gh pr merge --merge`). Do not wait for CI on
   the pull request or for a review, and never leave a green pull request open.
2. **CI runs on `main`** (`.github/workflows/ci.yml`). Whoever merged watches that run and fixes forward or reverts
   within the same round if it fails.
3. Update with `main` once, right before the local run. No second rebase unless GitHub reports a conflict.
4. **Reviews happen after the merge** and never hold a pull request. A finding becomes a follow-up pull request
   under the same rule.
5. **The host deploys itself, no model and no Mac:** `pixel-cd.timer` runs `deploy/cd.sh` every 30 s as root on
   the live host. It fetches `origin/main` into its own clone `/opt/pixel/cd/repo` (read-only deploy key of the user
   pixel) and always takes the newest commit. When every changed path is under `docs/`, `bench/`, `tests/`,
   `.github/`, is a top-level `.md` file or belongs to the deploy job itself, it moves the live checkout without a
   restart. Otherwise it tests the commit in the clone (`uv sync --locked`, `uv run pytest -q`, `uv run ruff check .`);
   red is refused for good and sent to `PIXEL_ALERT_URL`. Green waits for `tools/window.py`, at most one restart per
   round unless a merge since the live commit has `URGENT` in its title, then moves the live checkout, `uv sync --locked --no-dev`,
   restarts and checks that the UI answers; if not, it goes back to the previous commit and alerts. Root runs only
   files installed by hand: the job as `/opt/pixel/cd/cd.sh` and the units in `/etc/systemd/system`. A merged change
   to `deploy/cd.sh`, `deploy/pixel-cd.*` or `deploy/pixel.service` only logs that it differs; install it on the host
   under the job's lock and by rename, never in place, because `sh` reads a running script as it goes:
   `sudo flock /opt/pixel/cd/lock sh -c 'install -m 755 <file> /opt/pixel/cd/cd.sh.new && mv /opt/pixel/cd/cd.sh.new /opt/pixel/cd/cd.sh'`. One line per decision in `/opt/pixel/cd/cd.log`; `touch /opt/pixel/cd/PAUSE` pauses
   it. A fix for a round we just lost may go live from its branch first (pause the job, `tools/deploy.sh`); it is
   merged right after.

`tools/kpi_pr.sh` measures a branch against `main` on the live host with the real portal and gateway.

## Glossary

- **RFQ:** request for quotation, a customer's mail asking for a price for one air export shipment.
- **Session:** one customer thread with everything that belongs to it: the RFQ, our bid requests and the carriers'
  answers, the quote, the booking, tracking questions. Its id is the id of the thread's first mail.
- **Lane:** origin airport to destination airport, IATA codes.
- **Chargeable weight:** max(weight in kg, volume in cbm × 167). **Billed weight:** what the freight is charged on;
  it can be a higher break when that is cheaper.
- **Break, bracket:** a tariff's rate per kg applies from one weight break up to the next.
- **All-in total:** freight + fuel + security, plus pickup for door-to-port.
- **Option:** one priced way to ship the request (`RateOption`): from a sheet, the portal, a carrier's bid or the
  rate desk.
- **Bid:** a carrier's price by mail, in answer to our request. **Rate desk:** `ratedesk@hackloop.mail`.
- **Rate id:** the portal's id of one priced rate, valid 15 minutes, what we book with.
- **Team reference:** our CRM id of a shipment, sent with the booking.
- **Accepted:** by the *scorer*: our quote was within 5% of the best price. By the *customer*: they want to book.
  Say which one you mean.
- **Gateway:** 5U's proxy to OpenAI; it counts our spend.
- **Fallback:** the quote the orchestrator sends at the hard deadline, built by code from stored options.

## Ways to hurt yourself

- Replying to a customer with anything before the quote (an acknowledgement, a question): the next mail in that
  thread is no longer our first.
- Running a second live process against the mailbox: both answer, the first reply counts, the other burns mail
  limit and may book twice. Keep `PIXEL_DRY_RUN=1` unless you are the live process.
- Measuring a deadline from when we polled the mail. It runs from when the mail was stored.
- Using `float` for money or weight. Tariffs come with decimal commas as text and decimal points as numbers; parse
  into `Decimal` and round once, at the end.
- Assuming every airline has the same weight breaks, or that the bracket's own rate is the cheapest way to bill
  the weight.
- Putting the minimum on the total. It applies to the freight before surcharges; surcharges go on the billed
  weight.
- Spending rate reads on airlines that cannot win: reads are capped per minute, and `ports` and `brackets` are
  free.
- Booking with a `rate_id` older than 15 minutes: 410. Fetch a fresh rate and book that.
- Calling the model "just to be sure" in a loop or per carrier answer: the hourly spend is divided by *accepted*
  quotes only.
- Switching `PIXEL_MODEL` to a reasoning model without measuring it: reasoning tokens count against
  `max_completion_tokens`, and a small cap then returns no answer at all (`bench/MODELS.md`).
- Sending no gateway call at all for a quote: that quote earns no cost points.
- Treating the start kit as the truth for the weekend. It is one version; the tariff mails and the hourly changes
  replace it.
- Catching an exception in a handler and going on silently. Emit an `error` event; the fallback covers the quote.
- Mixing up the two meanings of "accepted" in a dashboard number.

## Keeping docs current

When behaviour changes, its docs change in the same commit: the agent's `README.md`, the row in `docs/RULES.md`
(status and where it lives), and the flow in `README.md` and plan §4 when the flow changes. Point to code by file
and function name, never by line number. Delete what is outdated instead of marking it. Everything in the repo is
written in English.
