# Reviews

One line per pull request and reviewed head commit. A pull request counts as reviewed only for that commit; a new
commit, or a later merge that touches the same function or rule, puts it back in the queue.

| PR | Author | Head | Verdict | The finding that mattered |
| --- | --- | --- | --- | --- |
| #453 | Konstantin-mody | 8b41a05 | ok | UI only; every page renders in `tools.preview`; the dashboard stays in the nav |
| #443 | lucakammerer | 4a7b98d | ok, fixed on the branch | a vetoed trucker price came back through the memory (265.00 for 260.00); fit moved to `calc.py`; unbounded scan, 0.70 s at 292k events, now 0.09 s; 0 of 111 door sessions had a real answer the estimate would replace |
| #445 | lucakammerer | dc27f4f | ok, fixed on the branch | a second gateway call per quote by default (kpi 69 to 79) and model text claiming an unpriced insurance in the quote; now off by default, reads and alerts only |
| #449 | lucakammerer | 4532f5b | closed, superseded by #452 | an old version priced unscaled, 3000.00 for 3900.00; drift check and read order left for a new PR |
| #452 | lucakammerer | 3abc6a8 | ok, fixed on the branch | the card priced an airline the current version found off the portal (404) from a scaled v1 rate, on `main` too; a card failure ended the TaskGroup; replay gate 19 of 20, round 159 (recovery) differs on `main` too |
| #452 | lucakammerer | 3abc6a8 | ok, fixed on the branch | the card priced an airline the current version found off the portal (404) from a scaled v1 rate, 3900.00 for none, on `main` too; a card failure ended the TaskGroup; replay gate 19 of 20, round 159 (recovery) differs on `main` too |
| #157 | lucakammerer | ffcd8f8 | ok (mine) | live old request and reply subject moved from tools/ to `rfqs/live-mutations.json` |
| #156 | lucakammerer | aa6886a | ok (mine) | a truck's load, a pallet rating or an earlier shipment no longer blocks a correct weight |
| #153 | Konstantin-mody | 6a6bc04 | fixed (#157) | 21 hand labels match their mails, independent of code output (round 9: 1952, not the 1750 priced live); offline run reproducible; live lanes and weights written into tools/ |
| #151 | Konstantin-mody | 06ab588 | fixed (#156) | no runtime cost (0.2 ms, never holds a quote); 36 live requests: only round 9 blocks, rightly; a heavier non-shipment figure ("carry 3 t", "last time 800 kg") blocked a correct weight |
| #147 | lucakammerer | ea24ee8 | ok (mine) | `REFUSED`: an airline's refusal in our booking thread is no confirmation |
| #146, #139 | lucakammerer | ae6a3a0, 77e4db1 | ok | RUNS notes only |
| #145 | lucakammerer | b462562 | ok (mine) | router test for #142's two probes; fails before #142 |
| #144 | lucakammerer | 9c41871 | ok | "1 250 kg" is 1250 (was 250); "3 120 kg pallets" reads 3120, wrong before too (120) |
| #143 | lucakammerer | 704ec0c | fixed (#147) | "could not be confirmed" in our booking thread routed as a confirmation and marked the booking confirmed |
| #142 | lucakammerer | c1166d4 | ok | confirmation needs an airline on the carrier list or `PX-`: 23 of 23 live confirmations from listed airlines; no test read the probes (#145) |
| #141 | TripleCF | 8777f83 | ok | cost estimate only while the scorer's is due, no double count; dashboard only |
| #138 | lucakammerer | 46268de | ok, voice owner's call | passes `check.run`, prices unchanged; "Dear Jonas Kessler" (#111) gone, German Sie mails open "Hallo Jonas!" |
| #137 | lucakammerer | 7651e64 | ok | a booking by mail found by the `PX-` reference its confirmation names |
| #136 | lucakammerer | 29fa7fd | ok (mine) | plan Q6 states the live bid rule |
| #135 | lucakammerer | 5048723 | ok | round 28 test reads its options from a fixture, no carrier names in tests |
| #133 | lucakammerer | 77c435c | ok | round 28 lost too_high: cheapest asked, only the balanced airline bid competed and the 390.45 bid never did; now every airline bid competes by the wish, truckers stay balanced; the only live session with two airline bids |
| #126 | lucakammerer | dbe4409 | ok, a measured bet | tracking variant b; back to a without +10 on the next tracking round |
| #125 | lucakammerer | 2f35e55 | ok | the small-talk answer thanks once; `goods_of` removed, nothing called it |
| #124 | lucakammerer | 56f3b19 | ok | an opening only as a reaction to a phrase in the mail, else none; 2 of 2 live openings dropped, one filler; nit: a None body skips the gateway call (no cost points) |
| #123 | rayan-bandy | 1102f08 | ok | benchmark replays stored model answers, spends nothing |
| #122 | maxihuang123-svg | cb990a5 | ok | strict xfails record extract holes; currency words and symbols latent (32 of 32 live requests asked EUR) |
| #121 | lucakammerer | eac7952 | ok, superseded by #125 | "we are shipping …:" goods |
| #119, #120 | lucakammerer | 174a544, bbcad38 | second reviewer's own fixes | not re-reviewed |
| #117 | lucakammerer | 9ca8e7f | ok | second reviewer's ledger |
| #116 | lucakammerer | e9630bd | ok, a measured bet | tracking variant a drops the address line; roll back after the first answer without +10 |
| #115 | lucakammerer | f798435 | ok | asks for a missing volume too; no live request lacked one; Q9 caveat as #86 |
| #113 | lucakammerer | 40f03be | ok (second reviewer's area) | takes effect only once `/opt/pixel/cd/cd.sh` is installed by hand |
| #112 | lucakammerer | d9056e6 | ok | closes #107's finding: root runs only hand-installed files (checked on the host) |
| #111 | lucakammerer | 2e771c1 | ok | formal greeting with the whole signed name |
| #110 | TripleCF | e9de807 | ok | dashboard shows the scorer's total; a missing total no longer reads as 0 |
| #109 | lucakammerer | 1f71ea6 | ok (second reviewer's area) | negated acceptances never book; all 17 acceptances of #87 still book |
| #107 | lucakammerer | 3ecb7f9 | needs follow-up (deploy thread) | a merged cd.sh runs as root untested; every merge is live within a round |
| #106 | lucakammerer | 1feb72c | ok | two more stiff openers rejected; one-sentence rule kept |
| #105 | lucakammerer | ba16727 | ok (second reviewer's area) | company token inside a longer word; three-letter tokens can match unrelated words |
| #103 | lucakammerer | review/currency-from-99 | own | currency part of #99; exchange rate only from the request's own options |
| #102 | lucakammerer | 97217eb | ok | bench notes only |
| #101 | TripleCF | 8986943 | ok | score rows sorted by round as a number; read-only |
| #100 | lucakammerer | a43c003 | ok | wording only |
| #99 | maxihuang123-svg | eb02390 | currency merged (#103), voice needs author | a stale exchange rate from another session; voice reverses the voice thread's choices |
| #98 | lucakammerer | 7f563e0 | ok (second reviewer's area) | pickup postcode from the customer's answer |
| #97 | lucakammerer | review/inches-only-as-unit | own | "120x80x150 in total" read as inches: 70.8 cbm instead of 4.32 (#95) |
| #95 | lucakammerer | c819a1d | fixed (#97) | bare "in" before a word taken as inches |
| #94 | Konstantin-mody | 49cca71 | ok, merged | pre-send check only observes; BLOCK alerts (offline: air-only door quotes only) |
| #93 | lucakammerer | a8898f5 | ok | tracking sign-off reuses `compose.in_kind`: one register reading |
| #92 | lucakammerer | 6ed8188 | ok | wording only |
| #91 | lucakammerer | 14136e8 | ok | consistent with #88: names the desk's postcode for a remembered site |
| #86 | lucakammerer | 3190757 | ok, a bet | one question before the quote when the pickup place is missing: Q9 open, watch the first round it fires |
| #84 | lucakammerer | ca6080f | ok | small talk answered in code; `compose.check` still guards the total |
| #80 | lucakammerer | 5f7de3f | ok | bench notes only |
| #78 | lucakammerer | fa8ac11 | ok | deploy window shut in a request's first two minutes and until the answer is out |
| #77 | lucakammerer | 0441f6a | ok | bid wait from our ask, bounded by `hard_at` |
| #75 | lucakammerer | 3e6c45a | ok | wording only |
| #74 | lucakammerer | 575d3c5 | ok | bench notes only |
| #73 | lucakammerer | 2ff1bb6 | ok | booking mails greet as the quote did (kept through #59's merge) |
| #72 | lucakammerer | 4cf7c61 | ok | goods in the customer's words, lower case only: no code or name copied |
| #71 | lucakammerer | 34e4d7c | ok | wording only |
| #70 | lucakammerer | be11ec1 | ok | fixture only |
| #69 | lucakammerer | 9baf0fe | superseded by #86 | an earlier site was right once and wrong twice: now asked, not guessed |
| #66 | lucakammerer | a192a4d | ok | truckers asked with the zone's first postcode |
| #65 | lucakammerer | 40efca1 | ok | a figure the mail does not write is replaced by code's reading (P1) |
| #64 | lucakammerer | 1188447 | ok | deploy waits for `tools.window` |
| #88 | lucakammerer | review/tracking-relaxed | own | tracking lost shipments with a remembered site postcode or a replaced origin (#76) |
| #87 | lucakammerer | review/acceptances | own | "Bitte buchen Sie", "We accept the quote", "Please go ahead" read as other: no booking (#76) |
| #85 | lucakammerer | 2b7b594 | own | "3 working days" and "EUR 850.00 per shipment" dropped a priced bid (#76) |
| #76 | maxihuang123-svg | f9e1f41 | fixed after merge (#82, #85, #87, #88) | merged by its author unreviewed and went live; a failed quote send left the request unanswered |
| #81 | lucakammerer | 83a4d5f | ok | docs and fixture only: the balanced rule fits every round with a known pickup place |
| #82 | lucakammerer | 587985d | own, URGENT | a quote's claim survived a 503 or lost receipt: the request was never answered (#76) |
| #67 | TripleCF | 91c52e7 | ok, merged | UI only; endpoints unchanged, output escaped; CRM tabs re-read all quotes per event burst |
| #63 | maxihuang123-svg | 48403b3 | merged, superseded by #76 | all 11 live tracking questions still match; zone via `pickup.zone_of` |
| #59 | maxihuang123-svg | 41c6496 | fixed, merged | new decline words cancelled acceptances ("pass on the AWB", "leider nicht vor Montag", "not too high") |
| #52 | maxihuang123-svg | 0526334 | fixed, merged | the late-price wait held the offline KPI ~45 min; airport check unbounded before pricing; gateway text in events |
| #61 | lucakammerer | 2204bf9 | own | quoted older mail cut in two places with different rules (#55, #56) |
| #56 | lucakammerer | 5092872 | fixed (#61) | second history cut in compose (`own_text`) |
| #55 | lucakammerer | 673896a | ok | reply subject dropped also for the model; ">" bullets cut the mail |
| #53 | lucakammerer | 9fbca3f | own | the gateway's error text in an event can quote the OpenAI key (#48) |
| #51 | lucakammerer | aee4dfb | own | a dry run or a replay booked a replayed acceptance at the portal for real |
| #50 | lucakammerer | 87c783f | ok | router counts pounds and tonnes as a weight |
| #49 | lucakammerer | 3fd44d0 | ok | bench notes only |
| #48 | lucakammerer | 7a3234f | fixed (#53) | guess event stored the gateway's error text |
| #47 | lucakammerer | b966950 | ok | bench notes only |
| #46 | lucakammerer | 628b796 | ok | times_the_best default; pickup_rule's default follows it, #25's lead still holds |
| #45 | lucakammerer | 3fb5197 | own | "As you asked for the most economical option" on every default request (#42) |
| #44 | lucakammerer | c903dd0 | ok | pounds and tonnes only without a kg figure; same LB as #36 |
| #43 | lucakammerer | 5e46838 | own | "not the fastest" and "quickly send me a price" read as fastest (#38) |
| #42 | lucakammerer | f73725e | fixed (#45) | false claim the customer asked for the cheapest; register read twice (compose and tracking) |
| #41 | lucakammerer | 27879c9 | own | ledger |
| #40 | lucakammerer | 39e0e6d | ok | town zone only on exactly one named area |
| #38 | lucakammerer | 5e82221 | fixed (#43) | bare "quickly" and negated superlatives decided the wish |
| #39 | lucakammerer | 6456d22 | own | one ready-date reading for quote and tracking |
| #37 | lucakammerer | 233872b | own | "no pickup before 9:00" vetoed the door pickup (#33) |
| #36 | lucakammerer | 0623250 | ok | copied groups replace the model's total without a cross-check: a copied total line counts twice |
| #35 | lucakammerer | 64ae1e3 | own | DG note pointed to an attachment when the mail itself declared the goods (#34) |
| #34 | lucakammerer | a574704 | fixed (#35) | one DG detection on main; live and main agreed again |
| #33 | lucakammerer | 3e70d93 | fixed (#37) | `OWN_DELIVERY` took any "no pickup" as no door pickup |
| #32 | lucakammerer | 6103985 | own | "non-dangerous goods" was read as declared dangerous goods (#29) |
| #31 | lucakammerer | 78af9c4 | ok | language from code; no finding |
| #30 | lucakammerer | 43b4061 | superseded by #34 | live from a local branch while #29 detected the same goods on main |
| #29 | lucakammerer | 240963b | fixed (#32) | DG regex matched "non-dangerous goods" and "no dangerous goods." |
| #28 | lucakammerer | 9960885 | ok | zone airport only replaces an origin no zone trucks to |
| #27 | lucakammerer | 5ba0112 | fixed (#39) | tracking's own `ready_date` took a deadline as the ready date |
| #25 | lucakammerer | 8ea5c88 | own | pickup rule switched after one misread round (#15) |
| #24 | lucakammerer | ab867ec | ok | a customer's attachment routed the request as a tariff (round 10 quoted after 4 min 20 s) |
| #23 | lucakammerer | a955c5d | fixed | the only date in a request was taken as the ready date |
| #22 | lucakammerer | 6281d7f | own | a lot line the rules cannot read was left out of the sum (#18) |
| #20 | lucakammerer | 0a746c3 | fixed (#39) | see #27 |
| #19 | lucakammerer | 1f7e45f | ok | alerts only for repairable issues since start; a dead poller now shows only through the board |
| #18 | lucakammerer | 023d25a | fixed (#22) | 922 kg instead of 1822 kg when one lot line reads "one crate" |
| #17 | Konstantin-mody (commits) | dad508f | ok | one call per quote kept; prose cannot hold the quote |
| #15 | lucakammerer | 9a84afc | fixed (#25) | nothing stopped a wrong rule learnt from one round |
| #26 | lucakammerer | a119f6e | ok | per-piece weight multiplied only with each/per/à |
| #21 | lucakammerer | f39d2ab | ok | follow-ups #22, #33/#37 |
| #16 | lucakammerer | b86a121 | ok | alert carries no payload or key; no retry by design |
| #14 | lucakammerer | — | ok, superseded | default rule replaced by #46 |

## Second review

One pass over how the changes merged up to #92 fit together, on main at 6ed8188 (21.11., 18:15 CET). Every
captured request was replayed offline (`tools.kpi`): 8 answered at their pinned prices, none lost against main; the
14 unanswered have no sheet price offline and are unchanged. All 14 live tracking questions of the day and all 61
customer replies were replayed against the fixed code.

| PRs | Verdict | The finding that mattered |
| --- | --- | --- |
| #86 | fixed (#98) | a model-read door request dropped the customer's answer ("it is 51115"): no origin, request lost; in the replay the request itself was read as the answer and the wait ran on wall time, so `tools.kpi` never saw the 60 s |
| #86 | ok | the question goes out only for a missing place (6 warehouse fixtures, no other), once per session (outbox key and the `question` row), never claims the quote; the answer routes as `other`, also after the fallback; a request with its place waits not a second |
| #86, #77, #52 | ok | one door request with no place: question at ~1 s, answer ~10 s or guess at 60 s, bids asked then and collected until max(target_at, ask + 45 s) ≈ 106 s, quote ≤ ~114 s with nominal truckers; without a trucker price it waits for one until hard_at (315 s) and sends air-only there; the orchestrator cancels the task at hard_at, so the fallback always leaves before expiry |
| #59, #68, #76, #87 | fixed (#109, URGENT) | "we have decided not to go ahead", "Bitte buchen Sie nicht", "go ahead with a competitor", "once we confirm pickup" booked at the portal; "We would like to book", "Angebot angenommen" and a reply below the quote did not book; a decline word after the booking set a booked quote declined |
| #46, #81, #59 | ok | `pickup_rule` cannot switch on one round (RULE_LEAD 2); `_pickup_hours` misreads "10.5 hours" as 5 and "09:00 h" as 0, latent: every captured trucker writes whole hours |
| #63, #88, #91 | fixed (#105) | "Sonnberg Kaffeeroesterei" never matched sonnberg-roesterei, so any customer's same-lane shipment within 1% could be the answer; 14 of 14 live questions right before and after; no wait, no provisional answer, one answer per question |
| #67 | ok | every GET only reads; take over, send and release work end to end; customer text escaped everywhere; pages cost ~20 ms at today's size |
| #64, #78 | ok, superseded by the host CD | the window stays shut over the question, the bid wait and the fallback; the Mac job's rollback could not push an older commit, moot since `deploy/cd.sh` resets the checkout |
| #107 | fixed (#113) | URGENT was read from the newest merge only: an URGENT fix with any merge on top waited a round; takes effect once `/opt/pixel/cd/cd.sh` is installed by hand |

## The pre-send check against the scorer

`uv run python -m tools.check_precision <database>` on the live log of 22.11., 06:43 UTC: every finding of
`agents/rfq/check.py` since 21.11. 17:27 UTC (162 `quote.checked` events in 124 sessions, 110 without a finding)
against the verdict of its round. A round marked * had the finding only on a quote that was not scored (152: the
requote after the customer's change).

| Rule | Level | Fired | Accepted rounds | Not accepted, by the scorer's reason |
| --- | --- | --- | --- | --- |
| `language_sent` | block | 1 | 131 | — |
| `pickup` | block | 3 | — | too_low: 63, 64, 65 |
| `text_amounts` | block | 2 | 92, 127 | — |
| `total` | block | 1 | 92 | — |
| `volume_unexplained` | block | 4 | 152*, 153 | — |
| `weight_unexplained` | block | 4 | 57, 152* | one_of_two: 113 |
| `best_price` | warn | 40 | 25, 38, 41, 46, 49, 50, 53, 66, 71, 80, 84, 88, 92, 100, 101, 106, 128, 132, 136, 140, 143, 148, 152*, 155 | no_pdf_offer: 76; one_of_two: 150; too_high: 28, 130; too_low: 93, 120 |
| `volume_unexplained` | warn | 2 | 143 | — |
| `weight_unexplained` | warn | 6 | 103, 114, 117 | too_low: 63, 64, 65 |

A BLOCK alerts `PIXEL_ALERT_URL` (`feedback.attention`, `quote_doubtful`) and `repair.settle` acts on it where
`REPAIRS` has an entry. The 12 blocking events fell on 10 rounds, 6 of them accepted:

- Right: `pickup` on rounds 63 to 65 (the fallback without a trucker price, `too_low`; it has no repair) and
  `weight_unexplained` on 113 (one of two shipments priced; not repaired). `language_sent` on 131 was right about
  the mail, a Turkish request answered in English: the repair's translation dropped a figure and was not kept, the
  English mail left and was accepted.
- False: `weight_unexplained` on 57 (the packing list's weight) and 152, `volume_unexplained` on 152 and 153,
  `total` and `text_amounts` on 92 (the insurance premium), `text_amounts` on 127 (a further shipment's price). On
  153 the false block spent a `repair.critic` call (0.0014 USD, 0.8 s) on picking another volume for a right
  quote; only its unreadable answer kept the price.
- `best_price` warned on 30 rounds, 24 of them accepted and 2 rejected as `too_high`.
