"""The local preview seeds a database the UI can show, and refuses the configured one."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pixel.ui import crm, dashboard, home, routes
from tools import preview


def test_preview_seeds_every_page(tmp_path):
    ctx, _ = preview.build(tmp_path / "preview.sqlite3", reseed=True, count=30, seed=3)
    db = ctx.db
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 30
    assert db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] > 0
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE mode = 'human'").fetchone()[0] == 1
    assert ctx.settings.dry_run and not ctx.settings.api_key
    app = FastAPI()
    for module in (home, routes, dashboard, crm):
        app.include_router(module.router)
    app.state.ctx = ctx
    client = TestClient(app)
    for path in ("/", "/requests", "/dashboard", "/crm/customers", "/crm/quotes", "/crm/shipments"):
        assert client.get(path).status_code == 200, path
    assert "GmbH" in client.get("/crm/customers").text  # the company from the signature, not the address
    first, every = client.get("/requests").text, client.get("/requests", params={"more": "1"}).text
    assert first.count('data-href="/sessions/') == 8 and "See more" in first
    assert every.count('data-href="/sessions/') > 8 and "See more" not in every
    sid = db.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()[0]
    assert client.get(f"/sessions/{sid}").status_code == 200


def test_preview_refuses_the_configured_database():
    with pytest.raises(SystemExit):
        preview.build(preview.Path(preview.settings().db_path), reseed=False, count=1, seed=1)


def test_reseed_deletes_only_its_own_database(tmp_path):
    live = tmp_path / "pixel.sqlite3"
    live.write_text("live")
    preview.build(tmp_path / "pixel", reseed=True, count=1, seed=1)  # a typo'd --db that prefixes the live file
    assert live.read_text() == "live"


def test_start_page_photos_are_served_and_nothing_else(tmp_path):
    ctx, _ = preview.build(tmp_path / "preview.sqlite3", reseed=True, count=5, seed=3)
    app = FastAPI()
    app.include_router(home.router)
    app.state.ctx = ctx
    client = TestClient(app)
    for name in home.PHOTOS:
        resp = client.get(f"/assets/img/{name}")
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert client.get("/assets/img/..%2Fhome.py").status_code == 404
