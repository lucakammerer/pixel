"""Replay stored mail through the pipeline without sending anything.

uv run python -m tools.replay tests/fixtures/mails/<file>.json     # one mail
uv run python -m tools.replay --session 123                        # a whole session from the database
uv run python -m tools.replay --capture 123 > tests/fixtures/mails/rfq-door.json
"""


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
