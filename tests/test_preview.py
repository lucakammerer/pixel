"""The local preview seeds a database the UI can show, and refuses the configured one."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pixel.ui import crm, dashboard, routes
from tools import preview


def test_preview_seeds_every_page(tmp_path):
    ctx, _ = preview.build(tmp_path / "preview.sqlite3", reseed=True, count=30, seed=3)
    db = ctx.db
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 30
    assert db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] > 0
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE mode = 'human'").fetchone()[0] == 1
    assert ctx.settings.dry_run and not ctx.settings.api_key
    app = FastAPI()
    for module in (routes, dashboard, crm):
        app.include_router(module.router)
    app.state.ctx = ctx
    client = TestClient(app)
    for path in ("/", "/dashboard", "/crm/customers", "/crm/quotes", "/crm/shipments"):
        assert client.get(path).status_code == 200, path
    sid = db.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()[0]
    assert client.get(f"/sessions/{sid}").status_code == 200


def test_preview_refuses_the_configured_database():
    with pytest.raises(SystemExit):
        preview.build(preview.Path(preview.settings().db_path), reseed=False, count=1, seed=1)
