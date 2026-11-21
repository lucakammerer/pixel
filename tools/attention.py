"""What went wrong lately, newest first: the list a repair run starts from.

uv run python -m tools.attention --minutes 30

Missed, rejected and point-losing requests (with the service's own explanation where it found one), tracking
questions matched to no shipment, customer replies left to a person, errors, carrier bids not trusted as read.
Read-only.
"""

import argparse
import json

from pixel.config import settings
from pixel.core import db, feedback


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=30)
    conn = db.connect(settings().db_path)
    db.init(conn)
    for issue in feedback.attention(conn, parser.parse_args().minutes):
        print(json.dumps(issue, default=str))


if __name__ == "__main__":
    main()
