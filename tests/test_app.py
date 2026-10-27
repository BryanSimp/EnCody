import base64

import pytest
from fastapi.testclient import TestClient

from reencoder import app as app_mod, config
from reencoder.db import DB


@pytest.fixture
def client(media, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASS", "secret")
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    app = app_mod.create_app(db=DB(str(tmp_path / "t.db")), start_worker=False)
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}

ROUTES = [("get", "/"), ("get", "/api/state"), ("put", "/api/settings"), ("get", "/api/browse"),
          ("get", "/api/files"), ("get", "/api/history"), ("post", "/api/retry"), ("post", "/api/scan"),
          ("post", "/api/run"), ("post", "/api/stop"), ("get", "/api/logs"), ("get", "/static/app.js"),
          ("get", "/nope"), ("get", "/docs")]


def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_requires_auth(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers=BAD).status_code == 401


def test_authed_state(client):
    r = client.get("/api/state", headers=AUTH)
    assert r.status_code == 200 and "counts" in r.json()


def test_browse_and_traversal(client, media):
    r = client.get("/api/browse", headers=AUTH).json()
    assert [d["path"] for d in r["dirs"]] == [str(media)]
    r = client.get("/api/browse", params={"path": str(media)}, headers=AUTH).json()
    assert {d["name"] for d in r["dirs"]} == {"movies", "tvshows"}
    for bad in ("/etc", str(media) + "/../../..", "../"):
        assert client.get("/api/browse", params={"path": bad}, headers=AUTH).status_code == 400


def test_settings_validation(client, media):
    put = lambda body: client.put("/api/settings", json=body, headers=AUTH)
    assert put({"folders": ["/etc"]}).status_code == 400
    assert put({"codec": "x264"}).status_code == 400
    assert put({"schedule_start": "25:00"}).status_code == 400
    assert put({"nope": 1}).status_code == 400
    r = put({"folders": [str(media / "movies")], "quality": "27", "jellyfin_key": "abc"})
    assert r.status_code == 200
    j = r.json()
    assert j["folders"] == [str(media / "movies")] and j["quality"] == 27 and j["jellyfin_key"] == "********"
    # masked secret is not overwritten
    put({"jellyfin_key": "********"})
    assert client.app.state.db.settings()["jellyfin_key"] == "abc"


def test_refuses_to_start_without_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "UI_PASS", "")
    with pytest.raises(SystemExit):
        app_mod.create_app(db=DB(str(tmp_path / "t.db")), start_worker=False)
