# Mail fixtures

One JSON file per stored mail, exactly as `GET /v1/mailbox/messages` returned it, captured with
`uv run python -m tools.replay --capture <session id>`. Name them by what they test (`rfq-door-to-port.json`,
`bid-odd-format.json`). Customer addresses are 5U's simulated agents; no keys, no real people.
