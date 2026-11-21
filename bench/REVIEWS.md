# Reviews

One line per pull request and reviewed head commit. A pull request counts as reviewed only for that commit; a new
commit, or a later merge that touches the same function or rule, puts it back in the queue.

| PR | Author | Head | Verdict | The finding that mattered |
| --- | --- | --- | --- | --- |
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
