# Reviews

One line per pull request and reviewed head commit. A pull request counts as reviewed only for that commit; a new
commit, or a later merge that touches the same function or rule, puts it back in the queue.

| PR | Head | Verdict | The finding that mattered |
| --- | --- | --- | --- |
| #39 | 6456d22 | own | one ready-date reading for quote and tracking |
| #37 | 233872b | own | "no pickup before 9:00" vetoed the door pickup (#33) |
| #36 | 0623250 | ok | copied groups replace the model's total without a cross-check: a copied total line counts twice |
| #35 | 64ae1e3 | own | DG note pointed to an attachment when the mail itself declared the goods (#34) |
| #34 | a574704 | fixed (#35) | one DG detection on main; live and main agreed again |
| #33 | 3e70d93 | fixed (#37) | `OWN_DELIVERY` took any "no pickup" as no door pickup |
| #32 | 6103985 | own | "non-dangerous goods" was read as declared dangerous goods (#29) |
| #31 | 78af9c4 | ok | language from code; no finding |
| #30 | 43b4061 | superseded by #34 | live from a local branch while #29 detected the same goods on main |
| #29 | 240963b | fixed (#32) | DG regex matched "non-dangerous goods" and "no dangerous goods." |
| #28 | 9960885 | ok | zone airport only replaces an origin no zone trucks to |
| #27 | 5ba0112 | fixed (#39) | tracking's own `ready_date` took a deadline as the ready date |
| #26 | a119f6e | pending | |
| #25 | 8ea5c88 | own | pickup rule switched after one misread round (#15) |
| #24 | ab867ec | ok | a customer's attachment routed the request as a tariff (round 10 quoted after 4 min 20 s) |
| #23 | a955c5d | fixed | the only date in a request was taken as the ready date |
| #22 | 6281d7f | own | a lot line the rules cannot read was left out of the sum (#18) |
| #21 | f39d2ab | pending | |
| #20 | 0a746c3 | fixed (#39) | see #27 |
| #19 | 1f7e45f | ok | alerts only for repairable issues since start; a dead poller now shows only through the board |
| #18 | 023d25a | fixed (#22) | 922 kg instead of 1822 kg when one lot line reads "one crate" |
| #17 | dad508f | ok | one call per quote kept; prose cannot hold the quote |
| #16 | b86a121 | pending | |
| #15 | 9a84afc | fixed (#25) | nothing stopped a wrong rule learnt from one round |
| #14 | | pending | |
