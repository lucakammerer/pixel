# Runs

Our result for every request, newest first, written by `uv run python -m tools.board_me` from `GET /v1/board/me`.
Do not edit rows by hand; notes go into the last column.

| Round | Sent | Answered | Seconds | Verdict | Reason | Accuracy | Cost | Time | Bonus | Total | Booked | Commit | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Changes we noticed

One line per setup change (R33): when, what failed, what it was, the commit that fixed it.

| When | Symptom | Cause | Fix |
| --- | --- | --- | --- |

## Voice loop (R31, R30)

Board before: 9 rounds, `human` 0 for every team; our quote was a template with one model sentence, the tracking
answer a record dump with the customer's own address.

- 21.11. 17:10: tried tracking answers in sentences by the asker's name and register, with status, booking reference
  and the arrival from the ready date (#20); changed: no labels, no customer address; next: watch the tracking +10.
- 21.11. 17:20: tried answering the request's questions in the quote by code: earliest arrival from the ready date,
  one air waybill, drop-off cut-off (on top of #17's prose); changed: one paragraph more, same total and model call;
  next: the `human` column after it is live.

## Generalization loop

Three lines per scenario: what we tried, what changed, what is next.

- 21.11. 17:05, captured requests: round 9 was rejected as too low because the model added up three lots itself
  (1750 instead of 1952 kg). Main's #18 sums lot lines in code; #21 has rules read all 7 captured requests, door-to-port
  by destination and pickup zone, with no model reading them. The decline "we will not go ahead" was booked, a
  finding for `booking.py` `intent`. Next: wordings the rules misread instead of passing on.
- 21.11. 17:20, wording: "each 48 kg", "250 kg per pallet" and "à 30 kg" were read as the total, "not urgent" as
  fastest, two sets of dimensions as volume 0, bulleted groups not at all. The rules now multiply by the pieces,
  give an unsettled volume to the model and read bullets as lots. Next: the model path, which still adds
  up unlabelled groups itself; pounds; city names.
- 21.11. 17:35, setup change: since 15:55 requests carry a packing list or letter of instruction declaring
  dangerous goods; round 10 was rejected (`dg_docs_missing`). `extract.py` now finds the declaration in the mail or
  any attachment and marks the request; naming it in the quote is compose's. Next: labelled-bullet requests by rules,
  and the language read by code, not the model.
- 21.11. 18:00, model path: on the three-lot mail without its labels gpt-4.1-mini wrote 1094 kg, but copied each group
  right (4 gateway calls on the host). The model now copies the groups and code adds them up, pounds included; the
  labelled-bullet request (round 11) and "no pickup needed" are read by rules. Next: wishes ("speed matters more
  than price"), a currency other than EUR, a postcode outside the zones.
- 21.11. 18:25, wording and mail flow: wishes by wording (#38), pickup by town (#40), pounds and tonnes by code
  (this one; "gross weight: 480 lbs" was read as 480 kg). A new request sent as a reply in an old thread routes as
  customer_reply and `outbox.send_quote` sends one quote per session: a finding for the main loop, not yet seen live.
  Next: setup changes (portal 429/503/410, a new dataset_version) replayed offline.

- 21.11. 17:15 CET: board after round 12: human awards START Milan 3, MAKEANOTHERMD 1, Austrian Power 1, PIXEL 0.
  Round 11 went out in German to an English mail (language by the model, fixed on main since), round 12 to Petra
  was the first in the new voice. No tracking question since 16:55 CET, so the tracking total is still unmeasured.
- 21.11. 17:20 CET: tried the quote as a clerk writes it: greeting and sign-off in the customer's register, the name
  by rule when the model gives none, one short opening (an echo of the mail is dropped), validity as "17:22 today",
  chargeable weight only when above gross, no closing line, 3 to 4 phrasings per sentence; changed: wording only,
  same total, carrier, timing and one model call; next: the `human` column for the first round it is live.
