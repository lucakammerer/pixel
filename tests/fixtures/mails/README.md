# Mail fixtures

One JSON file per stored mail, exactly as `GET /v1/mailbox/messages` returned it, captured with
`uv run python -m tools.replay --capture <message id>`. Name them by what they test (`rfq-door-to-port.json`,
`bid-odd-format.json`). Customer addresses are 5U's simulated agents; no keys, no real people.

`synthetic-*.json` are written by hand in the shape `pixel/api/mailbox.py` `to_message` assumes, before the first
real poll. Replace each with a captured mail of the same kind as soon as one arrives, then delete the synthetic one.
Their thread ids point at our own mails that `tests/test_router.py` stores first: 900 a bid request, 901 the
quote, 902 a request to the rate desk, all in session 1001.

A fixture may carry `content_base64` on an attachment; `tools/replay.py` serves it instead of downloading.
