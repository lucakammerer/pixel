# Reviews

One line per pull request and reviewed head commit. A pull request counts as reviewed only for that commit; a new
commit, or a later merge that touches the same function or rule, puts it back in the queue.

| PR | Author | Head | Verdict | The finding that mattered |
| --- | --- | --- | --- | --- |
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
