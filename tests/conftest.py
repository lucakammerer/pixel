from pathlib import Path

import pytest

from pixel.core import db

START_KIT = Path(__file__).parent.parent / "data" / "start-kit"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(str(tmp_path / "test.sqlite3"))
    db.init(conn)
    yield conn
    conn.close()


@pytest.fixture
def start_kit() -> Path:
    return START_KIT
