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
